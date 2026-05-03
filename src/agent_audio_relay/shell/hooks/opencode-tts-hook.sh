#!/usr/bin/env bash
set -euo pipefail
# agent-audio-relay: OpenCode (Codex) TTS hook
#
# Long-running watcher that polls OpenCode sessions for new assistant
# messages, generates TTS audio, and drops it where the agent-audio-relay
# watcher picks it up and delivers to the playback target.
#
# On first run, seeds state from existing sessions so only NEW messages
# trigger TTS.
#
# Two TTS engines are supported, selected via OPENCODE_TTS_ENGINE:
#   edge    (default) — Microsoft Edge TTS via the `edge-tts` CLI. Free, no API key.
#   openai             — OpenAI TTS via the python `openai` SDK. Better voices, paid.
#                        Falls back to edge if the API call fails.
#
# Requirements:
#   common: jq, opencode CLI
#   edge:   edge-tts (pip/pipx)
#   openai: python3 with the `openai` package, OPENAI_API_KEY in the hook env
#
# Environment variables:
#   OPENCODE_TTS_ENABLED         0 to disable (default: 1)
#   OPENCODE_TTS_ENGINE          edge | openai (default: edge)
#   OPENCODE_TTS_VOICE           Voice name (default: en-US-AriaNeural for edge, marin for openai)
#   OPENCODE_TTS_EDGE_VOICE      Edge voice used as the openai-fallback voice (default: en-US-AriaNeural)
#   OPENCODE_TTS_OPENAI_PYTHON   Python interpreter with `openai` installed (default: python3)
#   OPENCODE_TTS_OPENAI_MODEL    OpenAI TTS model (default: gpt-4o-mini-tts)
#   OPENCODE_TTS_DROP_DIR        Audio drop dir (default: /tmp/tts-opencode)
#   OPENCODE_TTS_SESSIONS_DIR    Session diff dir (default: ~/.local/share/opencode/storage/session_diff)
#   OPENCODE_TTS_POLL_INTERVAL   Seconds between polls (default: 3)
#   OPENCODE_TTS_MAX_MESSAGE_AGE Skip messages older than this many seconds (default: 300)
#   RELAY_EDGE_TTS_BIN           Path to edge-tts binary (default: edge-tts)
#   RELAY_OPENCODE_BIN           Path to opencode binary (default: opencode)

[ "${OPENCODE_TTS_ENABLED:-1}" = "0" ] && exit 0

OPENCODE_BIN="${RELAY_OPENCODE_BIN:-opencode}"
TTS_DROP="${RELAY_TTS_DROP_BIN:-$(dirname "$0")/../bin/tts-drop}"
[ -x "$TTS_DROP" ] || TTS_DROP="tts-drop"

emit_args=(
    --tag opencode
    --engine "${OPENCODE_TTS_ENGINE:-edge}"
    --edge-voice "${OPENCODE_TTS_EDGE_VOICE:-en-US-AriaNeural}"
)
[ -n "${OPENCODE_TTS_VOICE:-}" ]         && emit_args+=(--voice "$OPENCODE_TTS_VOICE")
[ -n "${OPENCODE_TTS_OPENAI_MODEL:-}" ]  && emit_args+=(--openai-model "$OPENCODE_TTS_OPENAI_MODEL")
[ -n "${OPENCODE_TTS_OPENAI_PYTHON:-}" ] && emit_args+=(--openai-python "$OPENCODE_TTS_OPENAI_PYTHON")

DROP_DIR="${OPENCODE_TTS_DROP_DIR:-/tmp/tts-opencode}"
STATE_FILE="${OPENCODE_TTS_STATE_FILE:-/tmp/opencode-tts-state.tsv}"
MTIME_FILE="${OPENCODE_TTS_MTIME_FILE:-/tmp/opencode-tts-mtime.tsv}"
EXPORT_TIMEOUT="${OPENCODE_TTS_EXPORT_TIMEOUT:-20}"
SESSIONS_DIR="${OPENCODE_TTS_SESSIONS_DIR:-${HOME}/.local/share/opencode/storage/session_diff}"
LOG_FILE="${OPENCODE_TTS_LOG_FILE:-/tmp/opencode-tts.log}"
POLL_INTERVAL="${OPENCODE_TTS_POLL_INTERVAL:-3}"
MAX_MESSAGE_AGE="${OPENCODE_TTS_MAX_MESSAGE_AGE:-300}"

mkdir -p "$DROP_DIR"
mkdir -p "$(dirname "$STATE_FILE")"
mkdir -p "$(dirname "$LOG_FILE")"
mkdir -p "$(dirname "$MTIME_FILE")"
touch "$STATE_FILE"
touch "$MTIME_FILE"
touch "$LOG_FILE"

# shellcheck source=lib/denote-stem.sh
. "$(dirname "$0")/lib/denote-stem.sh"

log() {
  local line
  line=$(printf '[%s] %s\n' "$(date -u '+%Y-%m-%d %H:%M:%S')" "$*")
  printf '%s\n' "$line" | tee -a "$LOG_FILE"
}

cleanup_opencode_tmp_so() {
  # opencode export currently leaks temp native .so files in /tmp.
  # Keep /tmp from filling; the export process has exited by the time this runs.
  find /tmp -maxdepth 1 -type f -user "$(id -un)" -name '.*-00000000.so' -size +1M -delete 2>/dev/null || true
}

latest_final_answer() {
  local session_id="$1"

  timeout "$EXPORT_TIMEOUT" "$OPENCODE_BIN" export "$session_id" 2>/dev/null \
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

mtime_get() {
  local session_id="$1"
  local line

  line=$(grep -F "${session_id}"$'	' "$MTIME_FILE" | tail -n 1 || true)
  [ -n "$line" ] || return 1
  printf '%s\n' "${line#*$'	'}"
}

mtime_set() {
  local session_id="$1"
  local mtime="$2"
  local tmp

  tmp=$(mktemp)
  grep -Fv "${session_id}"$'	' "$MTIME_FILE" > "$tmp" || true
  printf '%s\t%s\n' "$session_id" "$mtime" >> "$tmp"
  mv "$tmp" "$MTIME_FILE"
}

seed_state() {
  local file session_id latest message_id

  log "Seeding watcher state from existing sessions"
  shopt -s nullglob
  for file in "$SESSIONS_DIR"/*.json; do
    session_id="$(basename "$file" .json)"
    file_mtime=$(stat -c %Y "$file" 2>/dev/null || echo 0)
    latest=$(latest_final_answer "$session_id" || true)
    mtime_set "$session_id" "$file_mtime"
    [ -n "$latest" ] || continue
    message_id=$(printf '%s\n' "$latest" | jq -r '.id')
    [ -n "$message_id" ] || continue
    state_set "$session_id" "$message_id"
    mtime_set "$session_id" "$file_mtime"
    log "Seeded session $session_id with message $message_id"
  done
  shopt -u nullglob
}

enqueue_tts() {
  local text="$1"
  local session_id="${2:-}"
  local args=("${emit_args[@]}" --drop-dir "$DROP_DIR" --kind stop)
  [ -n "$session_id" ] && args+=(--session "$session_id")
  [ -n "${RELAY_LOG_FILE:-$LOG_FILE}" ] && args+=(--log-file "${RELAY_LOG_FILE:-$LOG_FILE}")
  if printf '%s' "$text" | "$TTS_DROP" "${args[@]}" >/dev/null; then
    log "Queued TTS for session=$session_id"
  else
    log "TTS failed for session=$session_id"
  fi
}

if [ ! -s "$STATE_FILE" ]; then
  seed_state
fi

log "OpenCode TTS watcher started"

while true; do
  shopt -s nullglob
  for file in "$SESSIONS_DIR"/*.json; do
    session_id="$(basename "$file" .json)"
    file_mtime=$(stat -c %Y "$file" 2>/dev/null || echo 0)
    known_mtime=$(mtime_get "$session_id" || true)
    [ "$known_mtime" = "$file_mtime" ] && continue

    latest=$(latest_final_answer "$session_id" || true)
    if [ -z "$latest" ]; then
      mtime_set "$session_id" "$file_mtime"
      continue
    fi

    message_id=$(printf '%s\n' "$latest" | jq -r '.id')
    completed_ms=$(printf '%s\n' "$latest" | jq -r '.completed')
    text=$(printf '%s\n' "$latest" | jq -r '.text')
    [ -n "$message_id" ] || { mtime_set "$session_id" "$file_mtime"; continue; }
    [ -n "$completed_ms" ] || { mtime_set "$session_id" "$file_mtime"; continue; }

    known_id=$(state_get "$session_id" || true)
    if [ "$known_id" = "$message_id" ]; then
      mtime_set "$session_id" "$file_mtime"
      continue
    fi

    completed_s=$((completed_ms / 1000))
    now_s=$(date +%s)
    age_s=$((now_s - completed_s))
    if (( age_s > MAX_MESSAGE_AGE )); then
      log "Skipping stale message $message_id from $session_id (${age_s}s old)"
      state_set "$session_id" "$message_id"
      mtime_set "$session_id" "$file_mtime"
      continue
    fi

    log "Speaking message $message_id from $session_id"
    enqueue_tts "$text" "$session_id"
    state_set "$session_id" "$message_id"
    mtime_set "$session_id" "$file_mtime"
  done
  shopt -u nullglob
  cleanup_opencode_tmp_so

  sleep "$POLL_INTERVAL"
done
