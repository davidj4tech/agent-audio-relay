/**
 * agent-audio-relay: pi TTS extension
 *
 * Mirrors the codex / claude-code / opencode hooks in
 * https://github.com/davidj4tech/agent-audio-relay — fires when pi finishes
 * a prompt (`agent_end`), extracts the final assistant text, runs it through
 * a TTS engine, and drops the audio file into a directory watched by the
 * `agent-audio-relay` daemon.  The daemon then handles delivery (phone via
 * SSH+Termux, mpv, etc.).
 *
 * Engines (PI_TTS_ENGINE):
 *   openai (default) — POSTs to https://api.openai.com/v1/audio/speech
 *                      using OPENAI_API_KEY.  Voice: PI_TTS_VOICE (default "marin").
 *   edge             — Spawns the `edge-tts` CLI (free, no key).
 *                      Voice: PI_TTS_VOICE (default "en-US-AriaNeural").
 *
 * Other env vars:
 *   PI_TTS_ENABLED        "0" disables (default: enabled)
 *   PI_TTS_DROP_DIR       Drop dir (default: /tmp/tts-pi)
 *   PI_TTS_OPENAI_MODEL   OpenAI model (default: gpt-4o-mini-tts)
 *   PI_TTS_EDGE_BIN       edge-tts binary (default: edge-tts)
 *   PI_TTS_MAX_CHARS      Cap on text length sent to TTS (default: 4000)
 *
 * Place at ~/.pi/agent/extensions/agent-audio-relay-tts.ts for global pickup.
 */

import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";
import { spawn, spawnSync } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { userInfo } from "node:os";

// ---------- helpers ----------------------------------------------------------

function denoteSlug(s: string): string {
	return s
		.replace(/[^A-Za-z0-9-]/g, "-")
		.replace(/-+/g, "-")
		.replace(/^-|-$/g, "");
}

function utcStamp(): string {
	const d = new Date();
	const pad = (n: number) => String(n).padStart(2, "0");
	return (
		`${d.getUTCFullYear()}${pad(d.getUTCMonth() + 1)}${pad(d.getUTCDate())}` +
		`T${pad(d.getUTCHours())}${pad(d.getUTCMinutes())}${pad(d.getUTCSeconds())}`
	);
}

function tmuxSessionName(): string {
	if (!process.env.TMUX && !process.env.TMUX_PANE) return "";
	try {
		const r = spawnSync("tmux", ["display-message", "-p", "#{session_name}"], { encoding: "utf8" });
		if (r.status === 0) return (r.stdout || "").trim();
	} catch {}
	return "";
}

function makeStem(agent: string, kind: string, sessionOverride?: string): string {
	const ts = utcStamp();
	const sessionRaw = sessionOverride || process.env.PI_SESSION || tmuxSessionName() || "";
	const session = denoteSlug(sessionRaw) || "pi";
	const persona = denoteSlug(userInfo().username || "unknown") || "unknown";
	return `${ts}--${session}__${persona}_${denoteSlug(agent)}_${denoteSlug(kind)}`;
}

function stripMarkdown(text: string): string {
	return text
		.replace(/```[\s\S]*?```/g, " ")           // fenced code blocks
		.replace(/`([^`]*)`/g, "$1")               // inline code
		.replace(/^#{1,6}\s+/gm, "")               // headings
		.replace(/\*\*([^*]+)\*\*/g, "$1")          // bold
		.replace(/\*([^*]+)\*/g, "$1")              // italic
		.replace(/^\s*[-*+]\s+/gm, "")             // bullets
		.replace(/^\s*\|.*\|\s*$/gm, "")           // table rows
		.replace(/^\s*\|?[\s:|-]+\|\s*$/gm, "")    // table separators
		.replace(/!\[[^\]]*\]\([^)]*\)/g, "")      // images
		.replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")   // links → label only
		.replace(/[ \t]+/g, " ")
		.replace(/\n{3,}/g, "\n\n")
		.trim();
}

function lastAssistantText(messages: any[]): string {
	for (let i = messages.length - 1; i >= 0; i--) {
		const m = messages[i];
		const msg = m?.message ?? m;                // tolerate either shape
		if (msg?.role !== "assistant") continue;
		const content = msg.content;
		if (!Array.isArray(content)) continue;
		const parts = content
			.filter((c: any) => c?.type === "text" && typeof c.text === "string")
			.map((c: any) => c.text as string);
		if (parts.length) return parts.join("\n");
	}
	return "";
}

// ---------- TTS engines -----------------------------------------------------

async function ttsOpenAI(text: string, outfile: string): Promise<void> {
	const key = process.env.OPENAI_API_KEY;
	if (!key) throw new Error("OPENAI_API_KEY not set");
	const model = process.env.PI_TTS_OPENAI_MODEL || "gpt-4o-mini-tts";
	const voice = process.env.PI_TTS_VOICE || "marin";
	const res = await fetch("https://api.openai.com/v1/audio/speech", {
		method: "POST",
		headers: {
			Authorization: `Bearer ${key}`,
			"Content-Type": "application/json",
		},
		body: JSON.stringify({ model, voice, input: text, response_format: "mp3" }),
	});
	if (!res.ok) {
		const body = await res.text().catch(() => "");
		throw new Error(`openai tts http ${res.status}: ${body.slice(0, 200)}`);
	}
	const buf = Buffer.from(await res.arrayBuffer());
	writeFileSync(outfile, buf);
}

function ttsEdge(text: string, outfile: string): Promise<void> {
	const bin = process.env.PI_TTS_EDGE_BIN || "edge-tts";
	const voice = process.env.PI_TTS_VOICE || "en-US-AriaNeural";
	return new Promise((resolve, reject) => {
		const p = spawn(bin, ["--text", text, "--voice", voice, "--write-media", outfile], {
			stdio: "ignore",
		});
		p.on("error", reject);
		p.on("exit", (code) => (code === 0 ? resolve() : reject(new Error(`edge-tts exit ${code}`))));
	});
}

// ---------- extension entrypoint -------------------------------------------

export default function (pi: ExtensionAPI) {
	pi.on("agent_end", async (event: any, _ctx) => {
		try {
			if (process.env.PI_TTS_ENABLED === "0") return;

			const raw = lastAssistantText(event?.messages ?? []);
			if (!raw) return;

			const cleaned = stripMarkdown(raw);
			if (!cleaned) return;

			const cap = Number(process.env.PI_TTS_MAX_CHARS) || 4000;
			const text = cleaned.length > cap ? cleaned.slice(0, cap) + " …" : cleaned;

			const dropDir = process.env.PI_TTS_DROP_DIR || "/tmp/tts-pi";
			mkdirSync(dropDir, { recursive: true });

			const engine = (process.env.PI_TTS_ENGINE || "openai").toLowerCase();
			const outfile = join(dropDir, `${makeStem("pi", "stop")}.mp3`);

			if (engine === "edge") {
				await ttsEdge(text, outfile);
			} else if (engine === "openai") {
				await ttsOpenAI(text, outfile);
			} else {
				// Unknown engine: silent no-op (don't break the agent)
				return;
			}
		} catch {
			// Never let TTS break the agent loop.
		}
	});
}
