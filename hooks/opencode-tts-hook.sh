#!/usr/bin/env bash
set -euo pipefail
# agent-audio-relay: OpenCode (Codex) TTS hook
#
# Long-running watcher that polls OpenCode sessions for new final_answer
# messages, generates Edge TTS audio, and drops it where the agent-audio-relay
# watcher picks it up and delivers to the phone.
#
# On first run, seeds state from existing sessions so only NEW messages
# trigger TTS.
#
# Requirements: edge-tts (pip/pipx), jq, opencode CLI
#
# Environment variables:
#   OPENCODE_TTS_ENABLED      0 to disable (default: 1)
#   OPENCODE_TTS_VOICE        Edge TTS voice (default: en-US-AriaNeural)
#   OPENCODE_TTS_DROP_DIR     Audio drop dir (default: /tmp/tts-opencode)
#   OPENCODE_TTS_SESSIONS_DIR Session diff dir (default: ~/.local/share/opencode/storage/session_diff)
#   OPENCODE_TTS_POLL_INTERVAL Seconds between polls (default: 3)
#   OPENCODE_TTS_MAX_MESSAGE_AGE Skip messages older than this many seconds (default: 300)
#   RELAY_EDGE_TTS_BIN        Path to edge-tts binary (default: edge-tts)
#   RELAY_OPENCODE_BIN        Path to opencode binary (default: opencode)

[ "${OPENCODE_TTS_ENABLED:-1}" = "0" ] && exit 0

EDGE_TTS="${RELAY_EDGE_TTS_BIN:-edge-tts}"
OPENCODE_BIN="${RELAY_OPENCODE_BIN:-opencode}"
VOICE="${OPENCODE_TTS_VOICE:-en-US-AriaNeural}"
DROP_DIR="${OPENCODE_TTS_DROP_DIR:-/tmp/tts-opencode}"
STATE_FILE="${OPENCODE_TTS_STATE_FILE:-/tmp/opencode-tts-state.tsv}"
SESSIONS_DIR="${OPENCODE_TTS_SESSIONS_DIR:-${HOME}/.local/share/opencode/storage/session_diff}"
LOG_FILE="${OPENCODE_TTS_LOG_FILE:-/tmp/opencode-tts.log}"
POLL_INTERVAL="${OPENCODE_TTS_POLL_INTERVAL:-3}"
MAX_MESSAGE_AGE="${OPENCODE_TTS_MAX_MESSAGE_AGE:-300}"

mkdir -p "$DROP_DIR"
mkdir -p "$(dirname "$STATE_FILE")"
mkdir -p "$(dirname "$LOG_FILE")"
touch "$STATE_FILE"
touch "$LOG_FILE"

log() {
  local line
  line=$(printf '[%s] %s\n' "$(date -u '+%Y-%m-%d %H:%M:%S')" "$*")
  printf '%s\n' "$line" | tee -a "$LOG_FILE"
}

strip_markdown() {
  printf '%s\n' "$1" \
    | sed 's/```[a-zA-Z0-9_-]*//g; s/```//g' \
    | sed 's/^#{1,6} //g' \
    | sed 's/\*\*\([^*]*\)\*\*/\1/g' \
    | sed 's/\*\([^*]*\)\*/\1/g' \
    | sed 's/`\([^`]*\)`/\1/g' \
    | sed 's/^\s*[-*] //g' \
    | sed '/^[[:space:]]*$/d'
}

latest_final_answer() {
  local session_id="$1"

  "$OPENCODE_BIN" export "$session_id" 2>/dev/null \
    | sed '1{/^Exporting session: /d;}' \
    | jq -c '
        [
          .messages[]
          | select(.info.role == "assistant")
          | select(.info.time.completed != null)
          | {
              id: .info.id,
              completed: .info.time.completed,
              text: (
                [
                  .parts[]?
                  | select(.type == "text")
                  | select((.metadata.openai.phase // "") == "final_answer")
                  | .text
                ]
                | join("\n")
              )
            }
          | select(.text != "")
        ]
        | last // empty
      '
}

state_get() {
  local session_id="$1"
  local line

  line=$(grep -F "${session_id}"$'\t' "$STATE_FILE" | tail -n 1 || true)
  [ -n "$line" ] || return 1
  printf '%s\n' "${line#*$'\t'}"
}

state_set() {
  local session_id="$1"
  local message_id="$2"
  local tmp

  tmp=$(mktemp)
  grep -Fv "${session_id}"$'\t' "$STATE_FILE" > "$tmp" || true
  printf '%s\t%s\n' "$session_id" "$message_id" >> "$tmp"
  mv "$tmp" "$STATE_FILE"
}

seed_state() {
  local file session_id latest message_id

  log "Seeding watcher state from existing sessions"
  shopt -s nullglob
  for file in "$SESSIONS_DIR"/*.json; do
    session_id="$(basename "$file" .json)"
    latest=$(latest_final_answer "$session_id" || true)
    [ -n "$latest" ] || continue
    message_id=$(printf '%s\n' "$latest" | jq -r '.id')
    [ -n "$message_id" ] || continue
    state_set "$session_id" "$message_id"
    log "Seeded session $session_id with message $message_id"
  done
  shopt -u nullglob
}

enqueue_tts() {
  local text="$1"
  local clean tmpfile

  clean=$(strip_markdown "$text")
  if [ -z "$clean" ]; then
    log "Skipped empty text after markdown stripping"
    return 0
  fi

  tmpfile="${DROP_DIR}/voice-$(date +%s%N).mp3"
  if ! "$EDGE_TTS" --text "$clean" --voice "$VOICE" --write-media "$tmpfile" >/dev/null 2>&1; then
    log "edge-tts failed while generating audio"
    return 0
  fi
  log "Queued TTS: $tmpfile"
}

if [ ! -s "$STATE_FILE" ]; then
  seed_state
fi

log "OpenCode TTS watcher started"

while true; do
  shopt -s nullglob
  for file in "$SESSIONS_DIR"/*.json; do
    session_id="$(basename "$file" .json)"
    latest=$(latest_final_answer "$session_id" || true)
    [ -n "$latest" ] || continue

    message_id=$(printf '%s\n' "$latest" | jq -r '.id')
    completed_ms=$(printf '%s\n' "$latest" | jq -r '.completed')
    text=$(printf '%s\n' "$latest" | jq -r '.text')
    [ -n "$message_id" ] || continue
    [ -n "$completed_ms" ] || continue

    known_id=$(state_get "$session_id" || true)
    [ "$known_id" = "$message_id" ] && continue

    completed_s=$((completed_ms / 1000))
    now_s=$(date +%s)
    age_s=$((now_s - completed_s))
    if (( age_s > MAX_MESSAGE_AGE )); then
      log "Skipping stale message $message_id from $session_id (${age_s}s old)"
      state_set "$session_id" "$message_id"
      continue
    fi

    log "Speaking message $message_id from $session_id"
    enqueue_tts "$text"
    state_set "$session_id" "$message_id"
  done
  shopt -u nullglob

  sleep "$POLL_INTERVAL"
done
