/**
 * agent-audio-relay: pi STREAMING TTS extension
 *
 * Streaming sibling of pi-tts-extension.ts. Instead of waiting until the
 * assistant finishes (`agent_end`) and rendering the whole response in
 * one go, this extension subscribes to per-token deltas (`message_update`
 * with `assistantMessageEvent.type === "text_delta"`) and pipes them
 * into a `tts-stream` subprocess as they arrive.
 *
 * tts-stream segments the incoming text on sentence boundaries, renders
 * each segment to audio in parallel (bounded), and pushes the bytes into
 * an HTTP stream that mpv-voice plays continuously. Audio starts within
 * ~2-3s of the first sentence completing instead of waiting for the
 * whole response.
 *
 * Use this OR pi-tts-extension.ts, not both — they'd both react to the
 * same assistant message and you'd get duplicated audio.
 *
 * Place at ~/.pi/agent/extensions/agent-audio-relay-tts-stream.ts.
 *
 * Env vars:
 *   PI_TTS_STREAM_ENABLED  "0" disables (default: enabled)
 *   PI_TTS_STREAM_BIN      tts-stream binary (default: tts-stream from PATH)
 *   PI_TTS_STREAM_ENGINE   Override engine for this extension only
 *                          (edge|openai|qwen). Maps to RELAY_TTS_ENGINE
 *                          in the spawned subprocess so it can differ
 *                          from your shell-wide default. Useful when
 *                          you want pi to use qwen but llm to use openai.
 *   PI_TTS_STREAM_VOICE    Override voice (engine-specific name)
 *   PI_TTS_STREAM_DEBUG    "1" logs spawn args + per-delta byte counts
 *                          to ~/logs/pi-tts-stream-debug.log
 *
 * All other engine/voice/key config is inherited via tts-stream's own
 * env vars (RELAY_TTS_ENGINE, RELAY_OPENAI_VOICE, RELAY_QWEN_VOICE,
 * DASHSCOPE_API_KEY, OPENAI_API_KEY, etc.) — the PI_TTS_STREAM_*
 * overrides above only kick in when set.
 */

import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";
import { spawn, type ChildProcess } from "node:child_process";
import { appendFileSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { homedir } from "node:os";

const DEBUG_LOG = join(homedir(), "logs", "pi-tts-stream-debug.log");
function dbg(msg: string): void {
	if (process.env.PI_TTS_STREAM_DEBUG !== "1") return;
	try {
		mkdirSync(dirname(DEBUG_LOG), { recursive: true });
		appendFileSync(DEBUG_LOG, `[${new Date().toISOString()}] ${msg}\n`);
	} catch {
		/* swallow — logging must never break the agent */
	}
}

// One active tts-stream subprocess per pi turn. Concurrent assistant
// messages are unusual in pi (one model response per user turn), but
// if a new message_start arrives before the previous one ended we
// gracefully end the previous stream so the new one's pre-loadfile
// stop doesn't fight a still-open HTTP server.
let active: ChildProcess | null = null;

function endActive(): void {
	const p = active;
	active = null;
	if (!p) return;
	try {
		if (p.stdin && !p.stdin.destroyed) {
			p.stdin.end();
		}
	} catch {
		/* swallow */
	}
}

export default function (pi: ExtensionAPI) {
	pi.on("message_start", (event: any, _ctx) => {
		try {
			if (process.env.PI_TTS_STREAM_ENABLED === "0") return;
			// Only stream the assistant's own messages — skip user
			// echoes, system inserts, and tool result messages.
			if (event?.message?.role !== "assistant") return;

			// If a previous stream is still open (concurrent turn), end
			// its stdin so it finalises before we spawn a new one. The
			// new tts-stream's own pre-loadfile `stop` to mpv-voice
			// will clear any leftover audio.
			endActive();

			const bin = process.env.PI_TTS_STREAM_BIN || "tts-stream";
			// --no-tee because pi already prints the assistant text in
			// its own TUI; teeing through tts-stream would duplicate.
			const args = ["--no-tee"];
			// Build subprocess env with optional pi-specific engine/voice
			// overrides. PI_TTS_STREAM_* takes precedence over inherited
			// RELAY_* so the user can keep llm on openai while pi uses
			// qwen, for instance.
			const env: NodeJS.ProcessEnv = { ...process.env };
			if (process.env.PI_TTS_STREAM_ENGINE) {
				env.RELAY_TTS_ENGINE = process.env.PI_TTS_STREAM_ENGINE;
			}
			if (process.env.PI_TTS_STREAM_VOICE) {
				// Each engine has its own voice env, so set them all —
				// only the engine actually used picks its own up.
				env.RELAY_EDGE_VOICE = process.env.PI_TTS_STREAM_VOICE;
				env.RELAY_OPENAI_VOICE = process.env.PI_TTS_STREAM_VOICE;
				env.RELAY_QWEN_VOICE = process.env.PI_TTS_STREAM_VOICE;
			}
			dbg(`spawn ${bin} ${args.join(" ")} engine=${env.RELAY_TTS_ENGINE || "(default)"} voice=${process.env.PI_TTS_STREAM_VOICE || "(default)"}`);
			active = spawn(bin, args, {
				stdio: ["pipe", "ignore", "ignore"],
				env,
			});
			active.on("error", () => {
				active = null;
			});
			active.on("exit", () => {
				// Don't null `active` here — endActive() may have
				// triggered the exit and already replaced it; checking
				// identity below avoids a TOCTOU clearing of a fresh
				// stream that just spawned.
			});
		} catch {
			active = null;
		}
	});

	pi.on("message_update", (event: any, _ctx) => {
		if (!active || !active.stdin || active.stdin.destroyed) return;
		const e = event?.assistantMessageEvent;
		// We only care about text content deltas. Skip thinking,
		// tool-call argument deltas, and start/end markers — those
		// don't belong in spoken output. Code block stripping is
		// handled by tts-stream's segmenter so we don't need to
		// pre-filter here.
		if (!e || e.type !== "text_delta" || typeof e.delta !== "string") return;
		dbg(`delta ${e.delta.length}b idx=${e.contentIndex} ${JSON.stringify(e.delta.slice(0, 30))}`);
		try {
			active.stdin.write(e.delta);
		} catch {
			/* if the producer closed early, drop the delta silently */
		}
	});

	pi.on("message_end", (_event: any, _ctx) => {
		dbg("message_end");
		endActive();
	});
}
