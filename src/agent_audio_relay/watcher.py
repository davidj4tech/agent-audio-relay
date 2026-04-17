"""TTS watcher daemon — the core of agent-audio-relay.

Watches directories for new audio files (mp3/opus/ogg/wav) via inotifywait,
queues them, pads 1s of silence (avoids Edge TTS last-word clipping), and
delivers them through the configured playback backend.

The active backend is resolved per file, so `agent-audio-relay switch <name>`
from another shell takes effect on the next queued audio without restarting
the daemon.

Environment variables:
    RELAY_BACKEND           Playback backend: ssh-termux, mpv (default: ssh-termux)
    RELAY_CONTROL_FILE      Control file for on-the-fly backend switching
                            (default: /tmp/agent-audio-relay-backend)
    RELAY_WATCH_DIRS        Colon-separated dirs to watch (default: /tmp/openclaw:/tmp)
    RELAY_QUEUE_DIR         Local queue directory (default: /tmp/agent-audio-relay-queue)
    RELAY_PAD_SILENCE       Pad 1s silence onto audio files: 1 or 0 (default: 1)

Subcommands:
    agent-audio-relay                   Run the watcher (default).
    agent-audio-relay switch <name>     Flip the active backend via control file.
    agent-audio-relay status            Print the currently-selected backend name.

See backend modules for backend-specific env vars.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

from .backends import PlaybackBackend
from .backends.registry import (
    CONTROL_FILE,
    KNOWN_BACKENDS,
    build_backend,
    resolve_backend_name,
)

WATCH_DIRS = os.environ.get("RELAY_WATCH_DIRS", "/tmp/openclaw:/tmp").split(":")
QUEUE_DIR = Path(os.environ.get("RELAY_QUEUE_DIR", "/tmp/agent-audio-relay-queue"))
STATE_FILE = Path("/tmp/agent-audio-relay-delivered.txt")
PAD_SILENCE = os.environ.get("RELAY_PAD_SILENCE", "1") == "1"

AUDIO_EXTS = {"mp3", "opus", "ogg", "wav"}


def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    print(f"[{ts}] {msg}", file=sys.stderr, flush=True)


def pad_audio(path: Path) -> None:
    """Append 1s of silence to avoid last-word clipping."""
    if not PAD_SILENCE:
        return

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


def process_queue(resolve: Callable[[], PlaybackBackend]) -> None:
    """Deliver all queued files in order, resolving the backend per file."""
    for queued in sorted(QUEUE_DIR.iterdir()):
        if not queued.is_file():
            continue
        backend = resolve()
        backend.wait_for_playback()
        pad_audio(queued)
        ok = backend.play(queued)
        log(f"{'PLAY:OK' if ok else 'PLAY:FAILED'} ({queued.name}) via {backend.name}")
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


def cmd_switch(name: str) -> int:
    name = name.lower().strip()
    if name not in KNOWN_BACKENDS:
        print(
            f"error: unknown backend {name!r}. Options: {', '.join(KNOWN_BACKENDS)}",
            file=sys.stderr,
        )
        return 2
    CONTROL_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONTROL_FILE.with_suffix(CONTROL_FILE.suffix + ".tmp")
    tmp.write_text(name + "\n")
    tmp.replace(CONTROL_FILE)
    print(f"switched to {name} ({CONTROL_FILE})")
    return 0


def cmd_status() -> int:
    name = resolve_backend_name()
    source = "control-file" if CONTROL_FILE.exists() and CONTROL_FILE.read_text().strip() else "env-default"
    print(f"{name} ({source})")
    return 0


def watch() -> None:
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.touch(exist_ok=True)

    for d in WATCH_DIRS:
        Path(d).mkdir(parents=True, exist_ok=True)

    cache: dict[str, PlaybackBackend] = {}
    current = [""]

    def resolve() -> PlaybackBackend:
        name = resolve_backend_name()
        if name not in cache:
            cache[name] = build_backend(name)
        if name != current[0]:
            if current[0]:
                log(f"BACKEND:SWITCH {current[0]} -> {name}")
            current[0] = name
        return cache[name]

    initial = resolve()
    log(
        f"Watcher started (dirs={WATCH_DIRS}, backend={initial.describe()}, "
        f"control={CONTROL_FILE})"
    )

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
        process_queue(resolve)
        trim_state()


def main() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] == "switch":
        if len(argv) != 2:
            print("usage: agent-audio-relay switch <backend>", file=sys.stderr)
            sys.exit(2)
        sys.exit(cmd_switch(argv[1]))
    if argv and argv[0] == "status":
        sys.exit(cmd_status())
    if argv and argv[0] not in ("watch",):
        print(
            "usage: agent-audio-relay [watch | switch <backend> | status]",
            file=sys.stderr,
        )
        sys.exit(2)
    watch()


if __name__ == "__main__":
    main()
