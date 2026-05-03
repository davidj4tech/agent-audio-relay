#!/bin/bash
set -euo pipefail
# agent-audio-relay: Codex (OpenAI CLI) TTS hook
#
# Generates speech with edge-tts when Codex finishes responding. Drops audio
# into a watched directory where agent-audio-relay picks it up and delivers
# to the phone.
#
# Codex pipes the assistant response text into the hook on stdin.
#
# Requirements: edge-tts (pip/pipx)
#
# Environment variables:
#   CODEX_TTS_ENABLED    0 to disable (default: 1)
#   CODEX_TTS_VOICE      Edge TTS voice (default: en-US-AriaNeural)
#   CODEX_TTS_DROP_DIR   Audio drop directory (default: /tmp/tts-codex)
#   RELAY_EDGE_TTS_BIN   Path to edge-tts binary (default: edge-tts)

[ "${CODEX_TTS_ENABLED:-1}" = "0" ] && exit 0

EDGE_TTS="${RELAY_EDGE_TTS_BIN:-edge-tts}"
VOICE="${CODEX_TTS_VOICE:-en-US-AriaNeural}"
DROP_DIR="${CODEX_TTS_DROP_DIR:-/tmp/tts-codex}"

mkdir -p "$DROP_DIR"

# shellcheck source=lib/denote-stem.sh
. "$(dirname "$0")/lib/denote-stem.sh"

input="$(cat)"
[ -n "$input" ] || exit 0

# Strip markdown formatting for cleaner speech
clean="$(
    printf '%s\n' "$input" \
        | sed 's/```[a-zA-Z0-9_-]*//g; s/```//g' \
        | sed 's/^#{1,6} //g' \
        | sed 's/\*\*\([^*]*\)\*\*/\1/g' \
        | sed 's/\*\([^*]*\)\*/\1/g' \
        | sed 's/`\([^`]*\)`/\1/g' \
        | sed 's/^\s*[-*] //g' \
        | sed '/^[[:space:]]*$/d'
)"

[ -n "$clean" ] || exit 0

# Generate audio and drop into watched directory
tmpfile="${DROP_DIR}/$(make_stem codex stop).mp3"
"$EDGE_TTS" --text "$clean" --voice "$VOICE" --write-media "$tmpfile" >/dev/null 2>&1 || exit 0
exit 0
