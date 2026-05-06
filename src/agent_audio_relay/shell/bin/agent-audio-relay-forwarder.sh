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

# Drain markers that landed while the forwarder was down, then start
# inotify. Both feed the same while-loop, so "marker = publish" stays
# the only contract anywhere — closes the migration-window hole where
# events fired before subscription would otherwise be lost forever.
{
  find "${WATCH_ROOTS[@]}" -maxdepth 1 -type f -name '*.play' 2>/dev/null
  inotifywait -m -q -e close_write -e moved_to --format '%w%f' "${WATCH_ROOTS[@]}"
} |
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
  # scp, not rsync: rsync's tmp-file+rename pattern doesn't fire
  # CLOSE_WRITE/MOVED_TO on Termux/Android, so the remote relay's
  # inotifywait never sees rsync-delivered files. Send audio *and*
  # marker in one scp invocation — scp processes sources in argv
  # order, so the marker's CLOSE_WRITE fires after the audio is in
  # place. Termux's `touch` uses utimensat() and silently fails to
  # fire CLOSE_WRITE on file creation, so ssh-touch doesn't work as
  # the publish signal; an actual write (scp) does.
  if scp -q "$audio" "$marker" "$REMOTE:$remote_dir/" 2>/dev/null; then
    rm -f "$audio" "$marker"
    echo "[forwarder] sent $audio -> $REMOTE:$remote_dir/" >&2
  else
    echo "[forwarder] FAILED $audio (will retry on next event)" >&2
  fi
done
