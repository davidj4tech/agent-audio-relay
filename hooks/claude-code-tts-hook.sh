#!/bin/bash
# agent-audio-relay: Claude Code TTS hook
#
# Generates speech when Claude Code finishes responding (Stop event) or
# receives a notification. Drops audio into a watched directory where the
# agent-audio-relay watcher picks it up and delivers to the phone.
#
# Two TTS engines are supported, selected via CLAUDE_TTS_ENGINE:
#   edge    (default) — Microsoft Edge TTS via the `edge-tts` CLI. Free, no API key.
#   openai             — OpenAI TTS via the python `openai` SDK. Better voices, paid.
#
# Also handles Notification events (input prompts) — prefixes with session name
# if running inside tmux.
#
# Requirements:
#   common: jq, tac
#   edge:   edge-tts (pip/pipx)
#   openai: python3 with the `openai` package, OPENAI_API_KEY in the hook env
#
# Claude Code config (~/.claude/settings.json):
#   Register as a Stop hook and optionally a Notification hook.

[ "$CLAUDE_TTS_ENABLED" = "0" ] && exit 0

ENGINE="${CLAUDE_TTS_ENGINE:-edge}"
DROP_DIR="${CLAUDE_TTS_DROP_DIR:-/tmp/tts-claude}"
STAMP_DIR="${CLAUDE_TTS_STAMP_DIR:-${TMPDIR:-/tmp}}"
mkdir -p "$STAMP_DIR" 2>/dev/null || true
mkdir -p "$DROP_DIR"

case "$ENGINE" in
    edge)
        EDGE_TTS="${RELAY_EDGE_TTS_BIN:-edge-tts}"
        VOICE="${CLAUDE_TTS_VOICE:-en-US-AriaNeural}"
        ;;
    openai)
        OPENAI_PYTHON="${CLAUDE_TTS_OPENAI_PYTHON:-python3}"
        OPENAI_MODEL="${CLAUDE_TTS_OPENAI_MODEL:-gpt-4o-mini-tts}"
        VOICE="${CLAUDE_TTS_VOICE:-marin}"
        ;;
    *)
        echo "claude-code-tts-hook: unknown CLAUDE_TTS_ENGINE='$ENGINE' (expected 'edge' or 'openai')" >&2
        exit 0
        ;;
esac

# tts_generate <text> <outfile>  — engine-agnostic; returns nonzero on failure.
tts_generate() {
    local text="$1" outfile="$2"
    case "$ENGINE" in
        edge)
            "$EDGE_TTS" --text "$text" --voice "$VOICE" --write-media "$outfile" 2>/dev/null
            ;;
        openai)
            TTS_TEXT="$text" TTS_OUTFILE="$outfile" TTS_MODEL="$OPENAI_MODEL" TTS_VOICE="$VOICE" \
                "$OPENAI_PYTHON" - <<'PY' 2>/dev/null
import os, sys
from openai import OpenAI
client = OpenAI()
with client.audio.speech.with_streaming_response.create(
    model=os.environ["TTS_MODEL"],
    voice=os.environ["TTS_VOICE"],
    input=os.environ["TTS_TEXT"],
) as resp:
    resp.stream_to_file(os.environ["TTS_OUTFILE"])
PY
            ;;
    esac
}

# shellcheck source=lib/denote-stem.sh
. "$(dirname "$0")/lib/denote-stem.sh"

input=$(cat)

# Handle Notification events (input prompts)
notification_msg=$(echo "$input" | jq -r '.message // empty')
if [ -n "$notification_msg" ]; then
    if [ -n "$TMUX_PANE" ]; then
        session=$(tmux display-message -p -t "$TMUX_PANE" '#{session_name}' 2>/dev/null)
        [ -n "$session" ] && notification_msg="Session ${session}: ${notification_msg}"
    fi

    # Debounce: skip if another notification fired within 2 min, or a Stop
    # played within 90 s (response audio is already on its way).
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

    tmpfile="${DROP_DIR}/$(make_stem claude notif).mp3"
    tts_generate "$notification_msg" "$tmpfile" || exit 0
    exit 0
fi

# Handle Stop events (Claude finished responding)
transcript_path=$(echo "$input" | jq -r '.transcript_path // empty')

if [ -z "$transcript_path" ] || [ ! -f "$transcript_path" ]; then
    exit 0
fi

# Wait briefly for transcript to be flushed to disk
sleep 0.5

# Extract all text blocks from the last assistant message that has text
text=$(tac "$transcript_path" \
    | jq -s '
        [.[] | select(.message.role == "assistant")
             | select([.message.content[]? | select(.type == "text")] | length > 0)
        ][0]
        | [.message.content[]? | select(.type == "text") | .text]
        | join("\n")
    ' -r 2>/dev/null)

if [ -z "$text" ]; then
    exit 0
fi

# Strip markdown formatting for cleaner speech
clean=$(echo "$text" \
    | sed 's/```[a-z]*//g; s/```//g' \
    | sed 's/^#{1,6} //g' \
    | sed 's/\*\*\([^*]*\)\*\*/\1/g' \
    | sed 's/\*\([^*]*\)\*/\1/g' \
    | sed 's/`\([^`]*\)`/\1/g' \
    | sed 's/^\s*[-*] //g' \
    | sed '/^[[:space:]]*$/d')

if [ -z "$clean" ]; then
    exit 0
fi

# Generate audio and drop into watched directory
tmpfile="${DROP_DIR}/$(make_stem claude stop).mp3"
tts_generate "$clean" "$tmpfile" || exit 0
date +%s > "$STAMP_DIR/claude-tts-stop-last"
exit 0
