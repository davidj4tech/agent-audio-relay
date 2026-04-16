"""Backend registry — resolves RELAY_BACKEND env var to an implementation."""

from __future__ import annotations

import os
import sys

from .base import PlaybackBackend


def get_backend() -> PlaybackBackend:
    """Return the configured playback backend.

    Set RELAY_BACKEND to one of: ssh-termux, mpv.
    Default: ssh-termux.
    """
    name = os.environ.get("RELAY_BACKEND", "ssh-termux").lower().strip()

    if name == "ssh-termux":
        from .ssh_termux import SshTermuxBackend
        return SshTermuxBackend()

    if name == "mpv":
        from .mpv import MpvBackend
        return MpvBackend()

    print(f"error: unknown backend {name!r}. Options: ssh-termux, mpv",
          file=sys.stderr)
    sys.exit(1)
