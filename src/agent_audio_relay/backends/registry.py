"""Backend registry — resolves backend name to an implementation.

Name resolution order:
    1. Control file at $RELAY_CONTROL_FILE (default /tmp/agent-audio-relay-backend),
       if present and non-empty. Lets `agent-audio-relay switch <name>` flip the
       active backend without restarting the watcher.
    2. $RELAY_BACKEND env var.
    3. Default: ssh-termux.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .base import PlaybackBackend

KNOWN_BACKENDS = ("ssh-termux", "mpv")
DEFAULT_BACKEND = "ssh-termux"
CONTROL_FILE = Path(os.environ.get("RELAY_CONTROL_FILE", "/tmp/agent-audio-relay-backend"))


def resolve_backend_name() -> str:
    """Return the currently-selected backend name.

    Reads the control file first so on-the-fly switches take effect; falls back
    to RELAY_BACKEND, then DEFAULT_BACKEND. Unknown names fall back to the env
    default rather than crashing the watcher mid-loop.
    """
    try:
        if CONTROL_FILE.exists():
            name = CONTROL_FILE.read_text().strip().lower()
            if name in KNOWN_BACKENDS:
                return name
    except OSError:
        pass
    name = os.environ.get("RELAY_BACKEND", DEFAULT_BACKEND).lower().strip()
    return name if name in KNOWN_BACKENDS else DEFAULT_BACKEND


def build_backend(name: str) -> PlaybackBackend:
    """Instantiate a backend by name."""
    if name == "ssh-termux":
        from .ssh_termux import SshTermuxBackend
        return SshTermuxBackend()
    if name == "mpv":
        from .mpv import MpvBackend
        return MpvBackend()
    print(f"error: unknown backend {name!r}. Options: {', '.join(KNOWN_BACKENDS)}",
          file=sys.stderr)
    sys.exit(1)


def get_backend() -> PlaybackBackend:
    """Return the currently-configured playback backend."""
    return build_backend(resolve_backend_name())
