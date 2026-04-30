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

EDGE_TTS="${RELAY_EDGE_TTS_BIN:-edge-tts}"
EDGE_VOICE="${CLAUDE_TTS_EDGE_VOICE:-en-US-AriaNeural}"

case "$ENGINE" in
    edge)
        VOICE="$EDGE_VOICE"
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
            local err_file
            err_file=$(mktemp)
            TTS_TEXT="$text" TTS_OUTFILE="$outfile" TTS_MODEL="$OPENAI_MODEL" TTS_VOICE="$VOICE" \
                "$OPENAI_PYTHON" - 2>"$err_file" <<'PY'
import os, sys
try:
    from openai import OpenAI
    client = OpenAI()
    with client.audio.speech.with_streaming_response.create(
        model=os.environ["TTS_MODEL"],
        voice=os.environ["TTS_VOICE"],
        input=os.environ["TTS_TEXT"],
    ) as resp:
        resp.stream_to_file(os.environ["TTS_OUTFILE"])
except Exception as e:
    msg = str(e)
    code = getattr(getattr(e, "response", None), "status_code", None) \
        or getattr(e, "status_code", None)
    name = type(e).__name__
    if code:
        sys.stderr.write(f"{name} ({code}): {msg}\n")
    else:
        sys.stderr.write(f"{name}: {msg}\n")
    sys.exit(1)
PY
            local rc=$?
            if [ $rc -ne 0 ] || [ ! -s "$outfile" ]; then
                local reason
                reason=$(tr '\n' ' ' < "$err_file" | sed 's/  */ /g; s/^ //; s/ $//')
                [ -z "$reason" ] && reason="unknown error"
                echo "claude-code-tts-hook: openai TTS failed: $reason" >&2
                local fail_stamp="$STAMP_DIR/claude-tts-openai-failed"
                if [ ! -f "$fail_stamp" ]; then
                    local short notice_final notice_stage
                    short=$(printf '%s' "$reason" | cut -c1-300)
                    notice_final="${DROP_DIR}/$(make_stem claude notice).mp3"
                    notice_stage=$(mktemp --suffix=.mp3)
                    if "$EDGE_TTS" --text "OpenAI TTS failed; falling back to Edge voice. Reason: ${short}" \
                            --voice "$EDGE_VOICE" --write-media "$notice_stage" 2>/dev/null \
                        && [ -s "$notice_stage" ]; then
                        mv "$notice_stage" "$notice_final"
                        touch "$fail_stamp"
                    fi
                    rm -f "$notice_stage"
                fi
                "$EDGE_TTS" --text "$text" --voice "$EDGE_VOICE" --write-media "$outfile" 2>/dev/null
            fi
            rm -f "$err_file"
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

    final="${DROP_DIR}/$(make_stem claude notif).mp3"
    staging=$(mktemp --suffix=.mp3)
    tts_generate "$notification_msg" "$staging" && [ -s "$staging" ] \
        && mv "$staging" "$final"
    rm -f "$staging"
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

# Debounce: skip if the same response text was just spoken (Stop hook can
# fire more than once per turn, which would double-play the audio).
hash_stamp="$STAMP_DIR/claude-tts-stop-last-hash"
text_hash=$(printf '%s' "$clean" | sha1sum | cut -d' ' -f1)
now=$(date +%s)
if [ -f "$hash_stamp" ]; then
    last_hash=$(cut -d' ' -f1 "$hash_stamp" 2>/dev/null)
    last_time=$(cut -d' ' -f2 "$hash_stamp" 2>/dev/null)
    if [ "$last_hash" = "$text_hash" ] && [ -n "$last_time" ] \
        && [ $(( now - last_time )) -lt 60 ]; then
        exit 0
    fi
fi
echo "$text_hash $now" > "$hash_stamp"

# Generate audio in a staging file outside the watched dir, then atomically
# rename in. This avoids the watcher seeing intermediate close_write events
# (e.g. openai streaming chunks, or fallback overwriting a failed openai file)
# which would enqueue and play the same response more than once.
final="${DROP_DIR}/$(make_stem claude stop).mp3"
staging=$(mktemp --suffix=.mp3)
if tts_generate "$clean" "$staging" && [ -s "$staging" ]; then
    mv "$staging" "$final"
    date +%s > "$STAMP_DIR/claude-tts-stop-last"
fi
rm -f "$staging"
exit 0
