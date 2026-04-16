#!/bin/bash
# agent-audio-relay: Claude Code TTS hook
#
# Generates speech with edge-tts when Claude Code finishes responding (Stop event)
# or receives a notification. Drops audio into a watched directory where the
# agent-audio-relay watcher picks it up and delivers to the phone.
#
# Also handles Notification events (input prompts) — prefixes with session name
# if running inside tmux.
#
# Requirements: edge-tts (pip/pipx), jq, tac
#
# Claude Code config (~/.claude/settings.json):
#   Register as a Stop hook and optionally a Notification hook.

[ "$CLAUDE_TTS_ENABLED" = "0" ] && exit 0

EDGE_TTS="${RELAY_EDGE_TTS_BIN:-edge-tts}"
VOICE="${CLAUDE_TTS_VOICE:-en-US-AriaNeural}"
DROP_DIR="${CLAUDE_TTS_DROP_DIR:-/tmp/tts-claude}"

mkdir -p "$DROP_DIR"

input=$(cat)

# Handle Notification events (input prompts)
notification_msg=$(echo "$input" | jq -r '.message // empty')
if [ -n "$notification_msg" ]; then
    if [ -n "$TMUX_PANE" ]; then
        session=$(tmux display-message -p -t "$TMUX_PANE" '#{session_name}' 2>/dev/null)
        [ -n "$session" ] && notification_msg="Session ${session}: ${notification_msg}"
    fi

    tmpfile="${DROP_DIR}/voice-$(date +%s%N).mp3"
    "$EDGE_TTS" --text "$notification_msg" --voice "$VOICE" --write-media "$tmpfile" 2>/dev/null || exit 0
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
tmpfile="${DROP_DIR}/voice-$(date +%s%N).mp3"
"$EDGE_TTS" --text "$clean" --voice "$VOICE" --write-media "$tmpfile" 2>/dev/null || exit 0
exit 0
