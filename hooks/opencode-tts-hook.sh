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

EDGE_TTS="${RELAY_EDGE_TTS_BIN:-edge-tts}"
OPENCODE_BIN="${RELAY_OPENCODE_BIN:-opencode}"
ENGINE="${OPENCODE_TTS_ENGINE:-edge}"
EDGE_VOICE="${OPENCODE_TTS_EDGE_VOICE:-en-US-AriaNeural}"
case "$ENGINE" in
    edge)
        VOICE="${OPENCODE_TTS_VOICE:-$EDGE_VOICE}"
        ;;
    openai)
        OPENAI_PYTHON="${OPENCODE_TTS_OPENAI_PYTHON:-python3}"
        OPENAI_MODEL="${OPENCODE_TTS_OPENAI_MODEL:-gpt-4o-mini-tts}"
        VOICE="${OPENCODE_TTS_VOICE:-marin}"
        ;;
    *)
        echo "opencode-tts-hook: unknown OPENCODE_TTS_ENGINE='$ENGINE'" >&2
        exit 1
        ;;
esac
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

# shellcheck source=lib/denote-stem.sh
. "$(dirname "$0")/lib/denote-stem.sh"

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

tts_generate() {
  local text="$1" outfile="$2"
  case "$ENGINE" in
    edge)
      "$EDGE_TTS" --text "$text" --voice "$VOICE" --write-media "$outfile" >/dev/null 2>&1
      ;;
    openai)
      local err_file rc
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
    sys.stderr.write(f"{name}{f' ({code})' if code else ''}: {msg}\n")
    sys.exit(1)
PY
      rc=$?
      if [ $rc -ne 0 ] || [ ! -s "$outfile" ]; then
        log "openai TTS failed: $(tr '\n' ' ' < "$err_file" | sed 's/  */ /g')"
        rm -f "$err_file"
        "$EDGE_TTS" --text "$text" --voice "$EDGE_VOICE" --write-media "$outfile" >/dev/null 2>&1
        return $?
      fi
      rm -f "$err_file"
      ;;
  esac
}

enqueue_tts() {
  local text="$1"
  local session_id="${2:-}"
  local clean final staging

  clean=$(strip_markdown "$text")
  if [ -z "$clean" ]; then
    log "Skipped empty text after markdown stripping"
    return 0
  fi

  final="${DROP_DIR}/$(make_stem opencode stop "$session_id").mp3"
  staging=$(mktemp --suffix=.mp3)
  if tts_generate "$clean" "$staging" && [ -s "$staging" ]; then
    mv "$staging" "$final"
    log "Queued TTS: $final"
  else
    log "TTS failed; no audio queued"
  fi
  rm -f "$staging"
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
    enqueue_tts "$text" "$session_id"
    state_set "$session_id" "$message_id"
  done
  shopt -u nullglob

  sleep "$POLL_INTERVAL"
done
