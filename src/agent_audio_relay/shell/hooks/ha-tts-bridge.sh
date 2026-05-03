#!/usr/bin/env bash
set -euo pipefail
# agent-audio-relay: Home Assistant → TTS bridge
#
# Listens to the HA SSE event stream for openclaw_message_received events,
# generates Edge TTS audio, and drops it where the agent-audio-relay watcher
# picks it up and delivers to the phone.
#
# Requirements: edge-tts (pip/pipx), curl, python3
#
# Environment variables:
#   HA_URL       Home Assistant URL (default: http://127.0.0.1:8123)
#   HA_TOKEN     Long-lived access token (required)
#   TTS_VOICE    Edge TTS voice (default: en-GB-SoniaNeural)
#   RELAY_EDGE_TTS_BIN  Path to edge-tts binary (default: edge-tts)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HA_URL="${HA_URL:-http://127.0.0.1:8123}"
HA_TOKEN="${HA_TOKEN:?Set HA_TOKEN to a long-lived access token}"
EDGE_TTS="${RELAY_EDGE_TTS_BIN:-edge-tts}"
VOICE="${TTS_VOICE:-en-GB-SoniaNeural}"
TTS_DIR="/tmp/openclaw/tts-ha"
MAX_LENGTH=3750

mkdir -p "$TTS_DIR"

# shellcheck source=lib/denote-stem.sh
. "$SCRIPT_DIR/lib/denote-stem.sh"

log() {
  echo "[$(date -u '+%Y-%m-%d %H:%M:%S')] $*"
}

log "HA TTS bridge started (voice=$VOICE, dir=$TTS_DIR)"

# Stream HA events via SSE, filter for openclaw_message_received
curl -sN \
  -H "Authorization: Bearer $HA_TOKEN" \
  -H "Accept: text/event-stream" \
  "$HA_URL/api/stream?restrict=openclaw_message_received" 2>/dev/null | \
while IFS= read -r line; do
  # SSE lines: "data: {json}"
  case "$line" in
    data:\ \{*)
      json="${line#data: }"
      text=$(echo "$json" | python3 -c "
import sys, json, re
try:
    d = json.load(sys.stdin)
    msg = d.get('data', {}).get('message', '')
    msg = re.sub(r'\*+', '', msg)
    msg = re.sub(r'\`\`\`[^\`]*\`\`\`', '', msg)
    msg = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', msg)
    msg = msg.strip()
    if msg:
        print(msg)
except Exception:
    pass
" 2>/dev/null)

      if [ -z "$text" ]; then
        continue
      fi

      # Truncate
      if [ "${#text}" -gt "$MAX_LENGTH" ]; then
        text="${text:0:$MAX_LENGTH}..."
      fi

      log "TTS: ${text:0:80}..."
      outfile="$TTS_DIR/$(make_stem ha announce).opus"
      if "$EDGE_TTS" --voice "$VOICE" --text "$text" --write-media "$outfile" 2>/dev/null; then
        log "TTS:OK ($outfile)"
      else
        log "TTS:FAILED"
      fi
      ;;
  esac
done
