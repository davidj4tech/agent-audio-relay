"""Backend registry — resolves a selector (control file / env / alias) to a
(backend-name, optional target) pair and instantiates the backend.

Selector forms accepted by `parse_selector`:
    mpv                     → ("mpv", None)
    ssh-termux              → ("ssh-termux", None)
    ssh-termux:AA:BB:CC:..  → ("ssh-termux", "AA:BB:CC:..")
    mpv:bluez_sink.x.a2dp   → ("mpv", "bluez_sink.x.a2dp_sink")
    <alias>                 → resolved via profiles.json (see load_profiles)

Name resolution for the *active* selector (used by the watcher loop):
    1. Control file at $RELAY_CONTROL_FILE (default
       $XDG_RUNTIME_DIR/agent-audio-relay/backend, falling back to
       /tmp/agent-audio-relay-backend-<uid>).
    2. $RELAY_BACKEND env var.
    3. DEFAULT_BACKEND.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Tuple

from .base import PlaybackBackend

KNOWN_BACKENDS = ("ssh-termux", "mpv")
DEFAULT_BACKEND = "mpv"


def _default_control_file() -> Path:
    """Per-user control file path.

    Prefers $XDG_RUNTIME_DIR (per-user, set by systemd) over /tmp to avoid
    cross-user collisions on shared hosts. Falls back to /tmp when no runtime
    dir is available.
    """
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / "agent-audio-relay" / "backend"
    return Path(f"/tmp/agent-audio-relay-backend-{os.getuid()}")


CONTROL_FILE = Path(os.environ.get("RELAY_CONTROL_FILE", str(_default_control_file())))
PROFILES_FILE = Path(
    os.environ.get(
        "RELAY_PROFILES_FILE",
        str(Path.home() / ".config" / "agent-audio-relay" / "profiles.json"),
    )
)

Selector = Tuple[str, "str | None"]

_profiles_warned = False


def load_profiles() -> dict[str, Selector]:
    """Return {alias: (backend, target)} from PROFILES_FILE, or {} if absent/invalid."""
    global _profiles_warned
    if not PROFILES_FILE.exists():
        return {}
    try:
        data = json.loads(PROFILES_FILE.read_text())
        aliases_raw = data.get("aliases", {})
        out: dict[str, Selector] = {}
        for alias, entry in aliases_raw.items():
            backend = entry.get("backend")
            if backend not in KNOWN_BACKENDS:
                continue
            target = entry.get("target") or None
            out[alias] = (backend, target)
        return out
    except (OSError, ValueError, AttributeError):
        if not _profiles_warned:
            print(f"agent-audio-relay: PROFILES:INVALID ({PROFILES_FILE})", file=sys.stderr)
            _profiles_warned = True
        return {}


def parse_selector(token: str) -> Selector | None:
    """Parse a selector string. Returns None if unrecognized."""
    token = token.strip()
    if not token:
        return None

    aliases = load_profiles()
    if token in aliases:
        return aliases[token]

    if ":" in token:
        backend, _, target = token.partition(":")
        backend = backend.lower().strip()
        target = target.strip() or None
        if backend in KNOWN_BACKENDS:
            return (backend, target)
        return None

    lowered = token.lower()
    if lowered in KNOWN_BACKENDS:
        return (lowered, None)
    return None


def resolve_selector() -> Selector:
    """Return the currently-active (backend, target). Control file > env > default."""
    try:
        if CONTROL_FILE.exists():
            raw = CONTROL_FILE.read_text().strip()
            if raw:
                parsed = parse_selector(raw)
                if parsed is not None:
                    return parsed
    except OSError:
        pass

    env = os.environ.get("RELAY_BACKEND", DEFAULT_BACKEND)
    parsed = parse_selector(env)
    if parsed is not None:
        return parsed
    return (DEFAULT_BACKEND, None)


def resolve_backend_name() -> str:
    """Back-compat helper — returns only the backend name."""
    return resolve_selector()[0]


def build_backend(name: str, target: str | None = None) -> PlaybackBackend:
    """Instantiate a backend by name, optionally with a target."""
    if name == "ssh-termux":
        from .ssh_termux import SshTermuxBackend
        return SshTermuxBackend(target=target)
    if name == "mpv":
        from .mpv import MpvBackend
        return MpvBackend(target=target)
    print(f"error: unknown backend {name!r}. Options: {', '.join(KNOWN_BACKENDS)}",
          file=sys.stderr)
    sys.exit(1)


def get_backend() -> PlaybackBackend:
    """Return the currently-configured playback backend."""
    name, target = resolve_selector()
    return build_backend(name, target)
