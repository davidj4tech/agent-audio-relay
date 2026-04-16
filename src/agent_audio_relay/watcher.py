#!/usr/bin/env python3
"""TTS watcher daemon — the core of agent-audio-relay.

Watches directories for new audio files (mp3/opus/ogg/wav) via inotifywait,
queues them, pads 1s of silence (avoids Edge TTS / termux-media-player last-word
clipping), SCPs to phone, and plays via termux-media-player.

Environment variables:
    RELAY_PHONE_HOST        SSH alias for the phone (default: p8ar)
    RELAY_PHONE_DEST        Remote path prefix (default: .cache/mel-latest)
    RELAY_WATCH_DIRS        Colon-separated dirs to watch (default: /tmp/openclaw:/tmp)
    RELAY_QUEUE_DIR         Local queue directory (default: /tmp/agent-audio-relay-queue)
    RELAY_MAX_RETRIES       SCP/play retry count (default: 2)
    RELAY_MAX_PLAYBACK_WAIT Max seconds to wait for current playback (default: 120)
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path


PHONE_HOST = os.environ.get("RELAY_PHONE_HOST", "p8ar")
PHONE_DEST = os.environ.get("RELAY_PHONE_DEST", ".cache/mel-latest")
WATCH_DIRS = os.environ.get("RELAY_WATCH_DIRS", "/tmp/openclaw:/tmp").split(":")
QUEUE_DIR = Path(os.environ.get("RELAY_QUEUE_DIR", "/tmp/agent-audio-relay-queue"))
STATE_FILE = Path("/tmp/agent-audio-relay-delivered.txt")
MAX_RETRIES = int(os.environ.get("RELAY_MAX_RETRIES", "2"))
MAX_PLAYBACK_WAIT = int(os.environ.get("RELAY_MAX_PLAYBACK_WAIT", "120"))

AUDIO_EXTS = {"mp3", "opus", "ogg", "wav"}


def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    print(f"[{ts}] {msg}", file=sys.stderr, flush=True)


def _mmss_to_s(mmss: str) -> int:
    parts = mmss.split(":")
    if len(parts) == 2:
        return int(parts[0]) * 60 + int(parts[1])
    return 0


def wait_for_playback() -> None:
    """Block until the phone finishes playing the current audio."""
    waited = 0
    while waited < MAX_PLAYBACK_WAIT:
        try:
            info = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3",
                 PHONE_HOST, "termux-media-player info"],
                capture_output=True, text=True, timeout=10,
            ).stdout
        except (subprocess.SubprocessError, OSError):
            break

        if "playing" not in info.lower():
            break

        # Parse position timestamps (MM:SS)
        times = re.findall(r"\d+:\d+", info)
        if len(times) >= 2:
            current = _mmss_to_s(times[0])
            total = _mmss_to_s(times[1])
            remaining = total - current
            if remaining <= 0:
                break
            wait_time = min(remaining + 1, MAX_PLAYBACK_WAIT - waited)
            log(f"Waiting {wait_time}s for current playback to finish")
            time.sleep(wait_time)
            waited += wait_time
        else:
            time.sleep(1)
            waited += 1


def pad_audio(path: Path) -> None:
    """Append 1s of silence to avoid last-word clipping."""
    sr = "24000"
    br = "48000"
    try:
        sr = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=sample_rate", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip() or sr
    except (subprocess.SubprocessError, OSError):
        pass
    try:
        br = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=bit_rate", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip() or br
    except (subprocess.SubprocessError, OSError):
        pass

    padded = path.with_suffix(f".padded{path.suffix}")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(path),
             "-f", "lavfi", "-t", "1", "-i", f"anullsrc=r={sr}:cl=mono",
             "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1",
             "-b:a", br, "-loglevel", "error", str(padded)],
            check=True, timeout=30,
        )
        padded.rename(path)
    except (subprocess.SubprocessError, OSError):
        log(f"PAD:SKIPPED (ffmpeg failed for {path})")
        padded.unlink(missing_ok=True)


def deliver_audio(path: Path) -> bool:
    """SCP file to phone and play it. Returns True on success."""
    ext = path.suffix.lstrip(".")
    dest = f"{PHONE_DEST}.{ext}"

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                 PHONE_HOST, "mkdir -p .cache"],
                check=True, capture_output=True, timeout=10,
            )
            subprocess.run(
                ["scp", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                 str(path), f"{PHONE_HOST}:{dest}"],
                check=True, capture_output=True, timeout=30,
            )
            subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                 PHONE_HOST, f"termux-media-player play '{dest}'"],
                check=True, capture_output=True, timeout=10,
            )
            log(f"PLAY:OK ({path.name})")
            return True
        except (subprocess.SubprocessError, OSError) as err:
            log(f"PLAY:FAILED (attempt {attempt}: {err})")
            if attempt < MAX_RETRIES:
                time.sleep(2)
    return False


def process_queue() -> None:
    """Deliver all queued files in order."""
    for queued in sorted(QUEUE_DIR.iterdir()):
        if not queued.is_file():
            continue
        wait_for_playback()
        pad_audio(queued)
        deliver_audio(queued)
        queued.unlink(missing_ok=True)


def enqueue_file(filepath: str) -> bool:
    """Copy an audio file into the delivery queue. Returns True if queued."""
    src = Path(filepath)
    ext = src.suffix.lstrip(".")
    if ext not in AUDIO_EXTS:
        return False

    # Skip already delivered
    if STATE_FILE.exists() and filepath in STATE_FILE.read_text():
        return False

    # Skip files older than 60s
    try:
        age = time.time() - src.stat().st_mtime
        if age > 60:
            return False
    except OSError:
        return False

    queue_entry = QUEUE_DIR / f"{time.time_ns()}.{ext}"
    try:
        shutil.copy2(str(src), str(queue_entry))
        log(f"Queued: {filepath} -> {queue_entry.name}")
        with open(STATE_FILE, "a") as f:
            f.write(filepath + "\n")
        return True
    except OSError:
        log(f"QUEUE:FAILED (cp {filepath})")
        return False


def trim_state() -> None:
    """Keep state file from growing indefinitely."""
    if not STATE_FILE.exists():
        return
    lines = STATE_FILE.read_text().splitlines()
    if len(lines) > 100:
        STATE_FILE.write_text("\n".join(lines[-50:]) + "\n")


def main() -> None:
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.touch(exist_ok=True)

    for d in WATCH_DIRS:
        Path(d).mkdir(parents=True, exist_ok=True)

    log(f"Watcher started (dirs={WATCH_DIRS}, phone={PHONE_HOST})")

    try:
        proc = subprocess.Popen(
            ["inotifywait", "-m", "-r",
             "-e", "close_write", "-e", "moved_to",
             "--format", "%w%f"] + WATCH_DIRS,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
    except FileNotFoundError:
        print("error: inotifywait not found. Install inotify-tools.", file=sys.stderr)
        sys.exit(1)

    assert proc.stdout is not None
    for line in proc.stdout:
        filepath = line.strip()
        # Only pick up files matching the tts-*/voice-* convention
        if "/tts-" not in filepath or "/voice-" not in filepath:
            continue
        enqueue_file(filepath)
        process_queue()
        trim_state()


if __name__ == "__main__":
    main()
