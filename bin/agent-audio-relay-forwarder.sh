#!/bin/bash
# Watch local /tmp/tts-* drop dirs and forward new audio clips to a remote
# host's agent-audio-relay watch dir over SSH. The remote relay then handles
# pad/queue/playback.
#
# Use this on a "sender" host (e.g. a headless Pi running Claude/Codex/etc.)
# when the playback daemon lives on a different machine (e.g. an Android phone
# in Termux running the relay against mpv-tts). It replaces a local
# agent-audio-relay daemon — do not run both, or every clip is delivered
# twice and mpv will restart the file mid-playback.
#
# Env:
#   RELAY_FWD_REMOTE        SSH alias of the playback host (default: p8ar)
#   RELAY_FWD_REMOTE_BASE   Remote watch root, relative to ~ (default: .cache/agent-audio-relay)
#   RELAY_FWD_WATCH_ROOTS   Space-separated drop dirs to watch
#                           (default: /tmp/tts-claude /tmp/tts-codex /tmp/tts-opencode /tmp/tts-ha)

set -u

REMOTE="${RELAY_FWD_REMOTE:-p8ar}"
REMOTE_BASE="${RELAY_FWD_REMOTE_BASE:-.cache/agent-audio-relay}"
read -r -a WATCH_ROOTS <<< "${RELAY_FWD_WATCH_ROOTS:-/tmp/tts-claude /tmp/tts-codex /tmp/tts-opencode /tmp/tts-ha}"

mkdir -p "${WATCH_ROOTS[@]}"

remote_dirs=""
for root in "${WATCH_ROOTS[@]}"; do
  remote_dirs+=" $REMOTE_BASE/$(basename "$root")"
done
ssh -o ConnectTimeout=5 "$REMOTE" "mkdir -p$remote_dirs" 2>/dev/null || true

echo "[forwarder] watching ${WATCH_ROOTS[*]} -> $REMOTE:$REMOTE_BASE/" >&2

inotifywait -m -q -e close_write -e moved_to --format '%w%f' "${WATCH_ROOTS[@]}" |
while IFS= read -r path; do
  case "$path" in
    *.mp3|*.opus|*.ogg|*.wav) ;;
    *) continue ;;
  esac
  [ -f "$path" ] || continue
  src_dir=$(basename "$(dirname "$path")")
  # scp, not rsync: rsync's tmp-file+rename pattern doesn't fire
  # CLOSE_WRITE/MOVED_TO on Termux/Android, so the remote relay's
  # inotifywait never sees rsync-delivered files.
  if scp -q "$path" "$REMOTE:$REMOTE_BASE/$src_dir/" 2>/dev/null; then
    rm -f "$path"
    echo "[forwarder] sent $path -> $REMOTE:$REMOTE_BASE/$src_dir/" >&2
  else
    echo "[forwarder] FAILED $path (will retry on next event)" >&2
  fi
done
