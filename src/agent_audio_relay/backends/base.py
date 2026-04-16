"""Base class for playback backends."""

from __future__ import annotations

from pathlib import Path


class PlaybackBackend:
    """Interface that all playback backends implement.

    A backend knows how to:
    - Wait for any current playback to finish (so messages sequence properly)
    - Play an audio file
    """

    name: str = "base"

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
