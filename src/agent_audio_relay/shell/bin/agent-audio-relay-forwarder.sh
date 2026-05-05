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
#                           (default: /tmp/tts-claude /tmp/tts-codex /tmp/tts-opencode /tmp/tts-ha /tmp/tts-llm)

set -u

REMOTE="${RELAY_FWD_REMOTE:-p8ar}"
REMOTE_BASE="${RELAY_FWD_REMOTE_BASE:-.cache/agent-audio-relay}"
read -r -a WATCH_ROOTS <<< "${RELAY_FWD_WATCH_ROOTS:-/tmp/tts-claude /tmp/tts-codex /tmp/tts-opencode /tmp/tts-ha /tmp/tts-llm}"

mkdir -p "${WATCH_ROOTS[@]}"

remote_dirs=""
for root in "${WATCH_ROOTS[@]}"; do
  remote_dirs+=" $REMOTE_BASE/$(basename "$root")"
done
ssh -o ConnectTimeout=5 "$REMOTE" "mkdir -p$remote_dirs" 2>/dev/null || true

echo "[forwarder] watching ${WATCH_ROOTS[*]} -> $REMOTE:$REMOTE_BASE/" >&2

inotifywait -m -q -e close_write -e moved_to --format '%w%f' "${WATCH_ROOTS[@]}" |
while IFS= read -r marker; do
  # Publish protocol: producers create `<audio>.play` *after* the audio
  # is at its final path. We only react to the marker, so non-published
  # files (e.g. tts-stream's concat archive) coexist in the watch dir
  # without ever being forwarded.
  case "$marker" in
    *.play) ;;
    *) continue ;;
  esac
  audio="${marker%.play}"
  case "$audio" in
    *.mp3|*.opus|*.ogg|*.wav) ;;
    *) rm -f "$marker"; continue ;;
  esac
  [ -f "$audio" ] || { rm -f "$marker"; continue; }
  src_dir=$(basename "$(dirname "$audio")")
  remote_dir="$REMOTE_BASE/$src_dir"
  remote_audio="$remote_dir/$(basename "$audio")"
  # scp, not rsync: rsync's tmp-file+rename pattern doesn't fire
  # CLOSE_WRITE/MOVED_TO on Termux/Android, so the remote relay's
  # inotifywait never sees rsync-delivered files. Emit the remote
  # marker via ssh-touch *after* the audio scp lands, mirroring the
  # local publish-after-rename ordering.
  if scp -q "$audio" "$REMOTE:$remote_dir/" 2>/dev/null \
       && ssh -o ConnectTimeout=5 "$REMOTE" "touch '$remote_audio.play'" 2>/dev/null; then
    rm -f "$audio" "$marker"
    echo "[forwarder] sent $audio -> $REMOTE:$remote_dir/" >&2
  else
    echo "[forwarder] FAILED $audio (will retry on next event)" >&2
  fi
done
