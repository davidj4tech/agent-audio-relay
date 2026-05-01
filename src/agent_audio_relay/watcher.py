"""TTS watcher daemon — the core of agent-audio-relay.

Watches directories for new audio files (mp3/opus/ogg/wav) via inotifywait,
queues them, pads 1s of silence (avoids Edge TTS last-word clipping), and
delivers them through the configured playback backend.

The active backend + target are resolved per file, so `agent-audio-relay
switch <name>` from another shell takes effect on the next queued audio
without restarting the daemon.

Selectors accepted by `switch`:
    <backend>                   e.g. mpv, ssh-termux
    <backend>:<target>          e.g. ssh-termux:AA:BB:CC:DD:EE:FF,
                                     mpv:bluez_sink.XX.a2dp_sink
    <alias>                     name defined in profiles.json

Environment variables:
    RELAY_BACKEND           Default selector (backend or backend:target) — used
                            when the control file is empty (default: ssh-termux)
    RELAY_CONTROL_FILE      Control file (default: $XDG_RUNTIME_DIR/agent-audio-relay/backend,
                            falling back to /tmp/agent-audio-relay-backend-<uid>)
    RELAY_PROFILES_FILE     Alias map (default: ~/.config/agent-audio-relay/profiles.json)
    RELAY_WATCH_DIRS        Colon-separated dirs to watch (default: /tmp/openclaw:/tmp)
    RELAY_QUEUE_DIR         Local queue directory (default: per-user under
                            $XDG_RUNTIME_DIR/agent-audio-relay/queue, with
                            fallbacks to $XDG_STATE_HOME or $TMPDIR/-<uid>)
    RELAY_STATE_FILE        Dedup ledger (default: <state-root>/delivered.txt)
    RELAY_PAD_SILENCE       Pad 1s silence onto audio files: 1 or 0 (default: 1)

Subcommands:
    agent-audio-relay                   Run the watcher (default).
    agent-audio-relay switch <sel>      Flip the active selector via control file.
    agent-audio-relay status            Print the current selector.
    agent-audio-relay list              Print known backends and configured aliases.
    agent-audio-relay scan [host]       SSH to host (default $RELAY_SSH_MPV_HOST or
                                        "homer"), list BT-capable PipeWire sinks,
                                        and emit suggested profiles.json aliases.

See backend modules for backend-specific env vars (including the BT-switch
hook `RELAY_TERMUX_SWITCH_CMD` used by the ssh-termux backend).
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
    PROFILES_FILE,
    build_backend,
    load_profiles,
    parse_selector,
    resolve_selector,
)

_TMP = os.environ.get("TMPDIR", "/tmp")
WATCH_DIRS = os.environ.get("RELAY_WATCH_DIRS", f"{_TMP}/openclaw:{_TMP}").split(":")


def _per_user_state_root() -> Path:
    """Per-user, writable directory for the relay's runtime state.

    Avoids the multi-user collision footgun where two users running their
    own relay would both default to a single canonical /tmp path; whoever
    started first owned it and the other got EACCES on the first touch().

    Preference order:
      1. $XDG_RUNTIME_DIR (per-user, set by systemd; cleared on logout)
      2. $XDG_STATE_HOME (per-user, persists across reboots)
      3. $TMPDIR/agent-audio-relay-<uid> as a last resort
    """
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / "agent-audio-relay"
    state = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    if state and Path(state).expanduser().parent.exists():
        return Path(state) / "agent-audio-relay"
    return Path(_TMP) / f"agent-audio-relay-{os.getuid()}"


_STATE_ROOT = _per_user_state_root()
QUEUE_DIR = Path(os.environ.get("RELAY_QUEUE_DIR", str(_STATE_ROOT / "queue")))
STATE_FILE = Path(os.environ.get("RELAY_STATE_FILE", str(_STATE_ROOT / "delivered.txt")))
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

    # Preserve original stem so backends can archive/replay by name.
    # Prepend ns to guarantee sort order even if two clips share a stem.
    queue_entry = QUEUE_DIR / f"{time.time_ns()}__{src.name}"
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


def _format_selector(backend: str, target: str | None) -> str:
    return f"{backend}:{target}" if target else backend


def cmd_switch(arg: str) -> int:
    parsed = parse_selector(arg)
    if parsed is None:
        aliases = load_profiles()
        known = list(KNOWN_BACKENDS) + [f"{b}:<target>" for b in KNOWN_BACKENDS]
        if aliases:
            known += [f"alias:{a}" for a in aliases]
        print(
            f"error: unrecognized selector {arg!r}. Options: {', '.join(known)}",
            file=sys.stderr,
        )
        return 2
    backend, target = parsed
    CONTROL_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONTROL_FILE.with_suffix(CONTROL_FILE.suffix + ".tmp")
    tmp.write_text(_format_selector(backend, target) + "\n")
    tmp.replace(CONTROL_FILE)
    print(f"switched to {_format_selector(backend, target)} ({CONTROL_FILE})")
    return 0


def cmd_status() -> int:
    backend, target = resolve_selector()
    source = "control-file" if CONTROL_FILE.exists() and CONTROL_FILE.read_text().strip() else "env-default"
    print(f"{_format_selector(backend, target)} ({source})")
    return 0


def cmd_scan(host: str | None) -> int:
    """SSH to a remote host and enumerate BT-capable PipeWire/Pulse sinks.

    Correlates `pactl list short sinks` with `bluetoothctl devices Paired` to
    map sink names to friendly device names. Prints suggested profiles.json
    entries so the user can paste them into their aliases config.
    """
    target_host = host or os.environ.get("RELAY_SSH_MPV_HOST", "homer")

    try:
        sinks_out = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
             target_host, "pactl list short sinks"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout
    except (subprocess.SubprocessError, OSError) as e:
        print(f"error: pactl on {target_host} failed ({e.__class__.__name__})",
              file=sys.stderr)
        return 1

    try:
        paired_out = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
             target_host, "bluetoothctl devices Paired 2>/dev/null || bluetoothctl paired-devices"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        paired_out = ""

    # Parse paired devices: "Device AA:BB:CC:DD:EE:FF Friendly Name"
    paired: dict[str, str] = {}
    for line in paired_out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) >= 3 and parts[0] == "Device":
            paired[parts[1].upper()] = parts[2]

    sinks: list[tuple[str, str | None, str | None]] = []
    for line in sinks_out.splitlines():
        cols = line.split("\t")
        if len(cols) < 2:
            continue
        sink_name = cols[1]
        mac: str | None = None
        friendly: str | None = None
        # Match bluez_output.XX_XX_XX_XX_XX_XX.1 or bluez_sink.XX_XX_XX_XX_XX_XX.a2dp_sink
        if "bluez_" in sink_name:
            for part in sink_name.split("."):
                if part.count("_") == 5 and len(part) == 17:
                    mac = part.replace("_", ":").upper()
                    friendly = paired.get(mac)
                    break
        sinks.append((sink_name, mac, friendly))

    print(f"sinks on {target_host}:")
    for sink_name, mac, friendly in sinks:
        label = friendly or ("(BT, not paired here)" if mac else "(local)")
        mac_str = f" [{mac}]" if mac else ""
        print(f"  {sink_name}{mac_str}  -- {label}")

    bt_sinks = [(n, m, f) for n, m, f in sinks if m]
    if bt_sinks:
        print()
        print("suggested profiles.json aliases:")
        print('{')
        print('  "aliases": {')
        lines = []
        for sink_name, mac, friendly in bt_sinks:
            alias = (friendly or mac or "bt").lower()
            alias = "".join(c if c.isalnum() else "-" for c in alias).strip("-")
            lines.append(
                f'    "{alias}": {{"backend": "ssh-mpv", "target": "{sink_name}"}}'
            )
        print(",\n".join(lines))
        print('  }')
        print('}')

    if not bt_sinks:
        print()
        print("no Bluetooth sinks currently active.")
        print(f"connect one first, e.g. on {target_host}:")
        print("  bluetoothctl connect <MAC>")
        if paired:
            print("paired devices:")
            for mac, name in sorted(paired.items()):
                print(f"  {mac}  {name}")
    return 0


def cmd_list() -> int:
    print("backends:")
    for b in KNOWN_BACKENDS:
        print(f"  {b}")
    aliases = load_profiles()
    print(f"aliases ({PROFILES_FILE}):")
    if not aliases:
        print("  (none)")
    else:
        width = max(len(a) for a in aliases)
        for alias, (backend, target) in sorted(aliases.items()):
            print(f"  {alias:<{width}}  -> {_format_selector(backend, target)}")
    return 0


def watch() -> None:
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.touch(exist_ok=True)

    for d in WATCH_DIRS:
        Path(d).mkdir(parents=True, exist_ok=True)

    cache: dict[tuple[str, str | None], PlaybackBackend] = {}
    current: list[tuple[str, str | None] | None] = [None]

    def resolve() -> PlaybackBackend:
        sel = resolve_selector()
        if sel not in cache:
            cache[sel] = build_backend(*sel)
        if sel != current[0]:
            if current[0] is not None:
                log(
                    f"BACKEND:SWITCH {_format_selector(*current[0])} -> "
                    f"{_format_selector(*sel)}"
                )
            current[0] = sel
        return cache[sel]

    initial = resolve()
    log(
        f"Watcher started (dirs={WATCH_DIRS}, backend={initial.describe()}, "
        f"control={CONTROL_FILE}, profiles={PROFILES_FILE})"
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
        # Only pick up files dropped into a tts-* directory
        # (denote-named or legacy voice-<ns> — both accepted)
        if "/tts-" not in filepath:
            continue
        enqueue_file(filepath)
        process_queue(resolve)
        trim_state()


def main() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] == "switch":
        if len(argv) != 2:
            print("usage: agent-audio-relay switch <selector>", file=sys.stderr)
            sys.exit(2)
        sys.exit(cmd_switch(argv[1]))
    if argv and argv[0] == "status":
        sys.exit(cmd_status())
    if argv and argv[0] == "list":
        sys.exit(cmd_list())
    if argv and argv[0] == "scan":
        host = argv[1] if len(argv) >= 2 else None
        sys.exit(cmd_scan(host))
    if argv and argv[0] not in ("watch",):
        print(
            "usage: agent-audio-relay [watch | switch <selector> | status | list | scan [host]]",
            file=sys.stderr,
        )
        sys.exit(2)
    watch()


if __name__ == "__main__":
    main()
