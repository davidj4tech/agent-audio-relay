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
        """Play an audio file. Returns True on success.

        The queued filename is `<ns>__<original-stem>.<ext>`. Backends that
        archive or expose a 'latest' pointer for replay should strip the
        `<ns>__` prefix via `original_name(path)` so the archived name
        matches the hook's denote-style stem.
        """
        raise NotImplementedError

    def describe(self) -> str:
        """Short human-readable description for log messages."""
        return self.name


def original_name(path: Path) -> str:
    """Strip the `<ns>__` queue prefix to recover the hook's filename."""
    name = path.name
    if "__" in name:
        prefix, rest = name.split("__", 1)
        if prefix.isdigit():
            return rest
    return name
