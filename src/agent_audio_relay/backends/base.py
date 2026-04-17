"""Base class for playback backends."""

from __future__ import annotations

from pathlib import Path


class PlaybackBackend:
    """Interface that all playback backends implement.

    A backend knows how to:
    - Wait for any current playback to finish (so messages sequence properly)
    - Play an audio file

    `target` is an optional backend-specific sink identifier — e.g. a BT MAC
    for ssh-termux, a PipeWire sink name for mpv. None means "backend default".
    """

    name: str = "base"
    target: str | None = None

    def wait_for_playback(self) -> None:
        """Block until any in-progress playback finishes.

        Backends that can't detect playback state should just return
        immediately — the queue will still deliver files in order.
        """

    def play(self, path: Path) -> bool:
        """Play an audio file. Returns True on success."""
        raise NotImplementedError

    def describe(self) -> str:
        """Short human-readable description for log messages."""
        return self.name
