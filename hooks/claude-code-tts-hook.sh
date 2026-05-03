#!/bin/bash
# agent-audio-relay: Claude Code TTS hook
#
# Generates speech when Claude Code finishes responding (Stop event) or
# receives a notification. Drops audio into a watched directory where the
# agent-audio-relay watcher picks it up and delivers to the playback target.
#
# Engine selection, edge/openai fallback, atomic-mv into the drop dir,
# and concurrent-invocation dedup all live in `tts-emit` — this hook
# is responsible only for finding the text to speak.
#
# Engine via CLAUDE_TTS_ENGINE (default: edge).
#
# Requirements:
#   common: jq, tac, tts-emit on PATH (or RELAY_TTS_EMIT_BIN)
#   edge:   edge-tts (pip/pipx)
#   openai: python3 with the `openai` package, OPENAI_API_KEY in the hook env

[ "${CLAUDE_TTS_ENABLED:-1}" = "0" ] && exit 0

DROP_DIR="${CLAUDE_TTS_DROP_DIR:-/tmp/tts-claude}"
STAMP_DIR="${CLAUDE_TTS_STAMP_DIR:-${TMPDIR:-/tmp}}"
mkdir -p "$STAMP_DIR" 2>/dev/null || true

TTS_EMIT="${RELAY_TTS_EMIT_BIN:-$(dirname "$0")/../bin/tts-emit}"
[ -x "$TTS_EMIT" ] || TTS_EMIT="tts-emit"

emit_args=(
    --tag claude
    --drop-dir "$DROP_DIR"
    --stamp-dir "$STAMP_DIR"
    --engine "${CLAUDE_TTS_ENGINE:-edge}"
    --edge-voice "${CLAUDE_TTS_EDGE_VOICE:-en-US-AriaNeural}"
)
[ -n "${CLAUDE_TTS_VOICE:-}" ]         && emit_args+=(--voice "$CLAUDE_TTS_VOICE")
[ -n "${CLAUDE_TTS_OPENAI_MODEL:-}" ]  && emit_args+=(--openai-model "$CLAUDE_TTS_OPENAI_MODEL")
[ -n "${CLAUDE_TTS_OPENAI_PYTHON:-}" ] && emit_args+=(--openai-python "$CLAUDE_TTS_OPENAI_PYTHON")
[ -n "${RELAY_LOG_FILE:-}" ]           && emit_args+=(--log-file "$RELAY_LOG_FILE")

input=$(cat)

# --- Notification events (input prompts) ---
notification_msg=$(echo "$input" | jq -r '.message // empty')
if [ -n "$notification_msg" ]; then
    if [ -n "${TMUX_PANE:-}" ]; then
        session=$(tmux display-message -p -t "$TMUX_PANE" '#{session_name}' 2>/dev/null)
        [ -n "$session" ] && notification_msg="Session ${session}: ${notification_msg}"
    fi

    # Skip if another notification fired within 2 min, or a Stop played
    # within 90 s (response audio is already on its way).
    notif_stamp="$STAMP_DIR/claude-tts-notif-last"
    stop_stamp="$STAMP_DIR/claude-tts-stop-last"
    now=$(date +%s)
    if [ -f "$notif_stamp" ]; then
        last=$(cat "$notif_stamp" 2>/dev/null)
        [ -n "$last" ] && [ $(( now - last )) -lt 120 ] && exit 0
    fi
    if [ -f "$stop_stamp" ]; then
        last_stop=$(cat "$stop_stamp" 2>/dev/null)
        [ -n "$last_stop" ] && [ $(( now - last_stop )) -lt 90 ] && exit 0
    fi
    echo "$now" > "$notif_stamp"

    printf '%s' "$notification_msg" | "$TTS_EMIT" "${emit_args[@]}" --kind notif
    exit 0
fi

# --- Stop events (Claude finished responding) ---
transcript_path=$(echo "$input" | jq -r '.transcript_path // empty')

if [ -z "$transcript_path" ] || [ ! -f "$transcript_path" ]; then
    exit 0
fi

# Tight retry while transcript flushes — saves ~0.4s on the common case.
transcript_mtime_ok() {
    local now last
    [ -s "$transcript_path" ] || return 1
    now=$(date +%s)
    last=$(stat -c %Y "$transcript_path" 2>/dev/null) || return 1
    [ $((now - last)) -le 5 ]
}
for _ in 1 2 3 4 5; do
    transcript_mtime_ok && break
    sleep 0.1
done

# Extract all text blocks from the last assistant message that has text.
text=$(tac "$transcript_path" \
    | jq -s '
        [.[] | select(.message.role == "assistant")
             | select([.message.content[]? | select(.type == "text")] | length > 0)
        ][0]
        | [.message.content[]? | select(.type == "text") | .text]
        | join("\n")
    ' -r 2>/dev/null)

[ -z "$text" ] && exit 0

# Hand off to tts-emit with --dedup-key so concurrent Stop invocations
# (duplicate Stop, or Stop + tail Notification) collapse atomically.
if printf '%s' "$text" | "$TTS_EMIT" "${emit_args[@]}" --kind stop --dedup-key "$text" >/dev/null; then
    date +%s > "$STAMP_DIR/claude-tts-stop-last"
fi
exit 0
