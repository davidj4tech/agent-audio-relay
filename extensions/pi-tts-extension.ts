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
 *   edge (default)  — Spawns the `edge-tts` CLI (free, no key).
 *                     Voice: PI_TTS_VOICE (default "en-US-AriaNeural").
 *   openai          — POSTs to https://api.openai.com/v1/audio/speech
 *                     using OPENAI_API_KEY. Voice: PI_TTS_VOICE (default "marin").
 *                     Falls back to edge if OpenAI TTS fails.
 *   piper           — Local Piper via the Wyoming TCP protocol. Sub-200ms
 *                     end-to-end synthesis on a typical clip. Writes WAV.
 *                     Env: PI_TTS_PIPER_HOST (default 100.125.48.108 / homer),
 *                          PI_TTS_PIPER_PORT (default 10200),
 *                          PI_TTS_PIPER_VOICE (default en_US-amy-medium).
 *                     Falls back to edge-mp3 if Piper is unreachable.
 *   qwen            — Alibaba DashScope Qwen TTS. High-quality natural voices.
 *                     Env: DASHSCOPE_API_KEY (required),
 *                          PI_TTS_QWEN_MODEL (default qwen3-tts-flash-2025-11-27;
 *                              the unversioned alias is unavailable on free-tier accounts),
 *                          PI_TTS_QWEN_VOICE (default Cherry; also Jada, Dylan),
 *                          PI_TTS_QWEN_LANG  (default English),
 *                          DASHSCOPE_BASE_URL (default https://dashscope-intl.aliyuncs.com/api/v1).
 *                     Falls back to edge-tts if DashScope fails.
 *
 * Other env vars:
 *   PI_TTS_ENABLED        "0" disables (default: enabled)
 *   PI_TTS_DROP_DIR       Drop dir (default: ~/.cache/agent-audio-relay/tts-pi)
 *   PI_TTS_OPENAI_MODEL   OpenAI model (default: gpt-4o-mini-tts)
 *   PI_TTS_EDGE_BIN       edge-tts binary (default: edge-tts)
 *   PI_TTS_MAX_CHARS      Cap on text length sent to TTS (default: 4000)
 *
 * Place at ~/.pi/agent/extensions/agent-audio-relay-tts.ts for global pickup.
 */

import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";
import { spawn, spawnSync } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { createConnection } from "node:net";
import { join } from "node:path";
import { hostname, userInfo } from "node:os";

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

// Slug the producing host's short hostname, lowercased — matches the
// bash and Python implementations elsewhere in the project. The host
// segment lets the playback side disambiguate same-named sessions
// across multiple producer hosts.
function hostSlug(): string {
	return denoteSlug(hostname().split(".")[0].toLowerCase()) || "unknown";
}

function makeStem(agent: string, kind: string, sessionOverride?: string): string {
	const ts = utcStamp();
	const sessionRaw = sessionOverride || process.env.PI_SESSION || tmuxSessionName() || "";
	const session = denoteSlug(sessionRaw) || "pi";
	const persona = denoteSlug(userInfo().username || "unknown") || "unknown";
	return `${ts}--${hostSlug()}--${session}__${persona}_${denoteSlug(agent)}_${denoteSlug(kind)}`;
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

function defaultDropDir(): string {
	return join(process.env.HOME || "/tmp", ".cache", "agent-audio-relay", "tts-pi");
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

async function ttsQwen(text: string, outfile: string): Promise<void> {
	const key = process.env.DASHSCOPE_API_KEY;
	if (!key) throw new Error("DASHSCOPE_API_KEY not set");
	const base = (process.env.DASHSCOPE_BASE_URL || "https://dashscope-intl.aliyuncs.com/api/v1").replace(/\/+$/, "");
	// DashScope only resolves the dated snapshot ids on free-tier accounts;
	// the unversioned alias 'qwen3-tts-flash' returns AccessDenied.Unpurchased.
	const model = process.env.PI_TTS_QWEN_MODEL || "qwen3-tts-flash-2025-11-27";
	const voice = process.env.PI_TTS_QWEN_VOICE || "Cherry";
	const language = process.env.PI_TTS_QWEN_LANG || "English";

	const res = await fetch(`${base}/services/aigc/multimodal-generation/generation`, {
		method: "POST",
		headers: { Authorization: `Bearer ${key}`, "Content-Type": "application/json" },
		body: JSON.stringify({ model, input: { text, voice, language_type: language } }),
	});
	if (!res.ok) {
		const body = await res.text().catch(() => "");
		throw new Error(`qwen tts http ${res.status}: ${body.slice(0, 200)}`);
	}
	const j: any = await res.json();
	if (j.code && j.code !== "Success" && !JSON.stringify(j).includes("\"url\"")) {
		throw new Error(`qwen dashscope error: ${JSON.stringify(j).slice(0, 200)}`);
	}
	const audioUrl = j?.output?.audio?.url || j?.output?.url || j?.url;
	if (!audioUrl) throw new Error("qwen tts response missing audio url");

	const audio = await fetch(audioUrl);
	if (!audio.ok) throw new Error(`qwen tts download http ${audio.status}`);
	writeFileSync(outfile, Buffer.from(await audio.arrayBuffer()));
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

// Wyoming protocol: \n-terminated JSON header, optionally followed by
// `data_length` bytes of inline JSON metadata, then `payload_length` bytes
// of binary payload. Modern piper-wyoming emits audio-start / audio-chunk /
// audio-stop events; PCM lives in audio-chunk.payload.
function ttsPiper(text: string, outfile: string): Promise<void> {
	const host = process.env.PI_TTS_PIPER_HOST || "100.125.48.108";
	const port = Number(process.env.PI_TTS_PIPER_PORT) || 10200;
	const voice = process.env.PI_TTS_PIPER_VOICE || "en_US-amy-medium";
	const timeoutMs = Number(process.env.PI_TTS_PIPER_TIMEOUT_MS) || 15000;

	return new Promise((resolve, reject) => {
		const sock = createConnection(port, host);
		sock.setTimeout(timeoutMs);

		let buf = Buffer.alloc(0);
		const chunks: Buffer[] = [];
		let rate = 22050, width = 2, channels = 1;
		let done = false;

		const finish = (err?: Error) => {
			if (sock.destroyed) return;
			sock.destroy();
			if (err) return reject(err);
			if (!chunks.length) return reject(new Error("piper produced no audio"));
			const pcm = Buffer.concat(chunks);
			const wav = wavWrap(pcm, rate, width, channels);
			try { writeFileSync(outfile, wav); resolve(); }
			catch (e) { reject(e as Error); }
		};

		sock.on("error", (e) => finish(e));
		sock.on("timeout", () => finish(new Error("piper timeout")));
		sock.on("close", () => { if (!done) finish(new Error("piper socket closed early")); });

		sock.on("connect", () => {
			const evt = { type: "synthesize", data: { text, voice: { name: voice } } };
			sock.write(JSON.stringify(evt) + "\n");
		});

		sock.on("data", (data) => {
			buf = Buffer.concat([buf, data]);
			while (true) {
				const nl = buf.indexOf(0x0a);
				if (nl < 0) return;
				let header: any;
				try { header = JSON.parse(buf.subarray(0, nl).toString("utf8")); }
				catch (e) { return finish(new Error("piper bad header")); }
				const dlen = Number(header.data_length) || 0;
				const plen = Number(header.payload_length) || 0;
				const need = nl + 1 + dlen + plen;
				if (buf.length < need) return;
				let cur = nl + 1;
				let inlineData: any = header.data || {};
				if (dlen) {
					try { inlineData = JSON.parse(buf.subarray(cur, cur + dlen).toString("utf8")); }
					catch { /* ignore */ }
					cur += dlen;
				}
				const payload = plen ? buf.subarray(cur, cur + plen) : null;
				cur += plen;
				buf = buf.subarray(cur);

				const t = header.type;
				if (t === "audio-start") {
					rate = inlineData.rate || rate;
					width = inlineData.width || width;
					channels = inlineData.channels || channels;
				} else if (t === "audio-chunk" && payload && payload.length) {
					chunks.push(Buffer.from(payload));
				} else if (t === "audio-stop") {
					done = true;
					finish();
					return;
				}
			}
		});
	});
}

function wavWrap(pcm: Buffer, rate: number, width: number, channels: number): Buffer {
	const byteRate = rate * channels * width;
	const blockAlign = channels * width;
	const header = Buffer.alloc(44);
	header.write("RIFF", 0);
	header.writeUInt32LE(36 + pcm.length, 4);
	header.write("WAVE", 8);
	header.write("fmt ", 12);
	header.writeUInt32LE(16, 16);              // fmt chunk size
	header.writeUInt16LE(1, 20);               // PCM
	header.writeUInt16LE(channels, 22);
	header.writeUInt32LE(rate, 24);
	header.writeUInt32LE(byteRate, 28);
	header.writeUInt16LE(blockAlign, 32);
	header.writeUInt16LE(width * 8, 34);
	header.write("data", 36);
	header.writeUInt32LE(pcm.length, 40);
	return Buffer.concat([header, pcm]);
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

			const dropDir = process.env.PI_TTS_DROP_DIR || defaultDropDir();
			mkdirSync(dropDir, { recursive: true });

			const engine = (process.env.PI_TTS_ENGINE || "edge").toLowerCase();
			const ext = (engine === "piper" || engine === "qwen") ? "wav" : "mp3";
			const outfile = join(dropDir, `${makeStem("pi", "stop")}.${ext}`);

			if (engine === "edge") {
				await ttsEdge(text, outfile);
			} else if (engine === "openai") {
				try {
					await ttsOpenAI(text, outfile);
				} catch {
					await ttsEdge(text, outfile);
				}
			} else if (engine === "piper") {
				try {
					await ttsPiper(text, outfile);
				} catch {
					// Fall back to edge-mp3 if Piper is unreachable
					await ttsEdge(text, outfile.replace(/\.\w+$/, ".mp3"));
				}
			} else if (engine === "qwen") {
				try {
					await ttsQwen(text, outfile);
				} catch {
					// Fall back to edge if DashScope fails
					await ttsEdge(text, outfile);
				}
			} else {
				// Unknown engine: silent no-op (don't break the agent)
				return;
			}
		} catch {
			// Never let TTS break the agent loop.
		}
	});
}
