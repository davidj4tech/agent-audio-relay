"""Playback backends for agent-audio-relay."""

from __future__ import annotations

from .base import PlaybackBackend
from .registry import get_backend

__all__ = ["PlaybackBackend", "get_backend"]
