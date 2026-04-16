"""Strip markdown formatting for cleaner TTS speech."""

from __future__ import annotations

import re


def strip_markdown(text: str) -> str:
    """Remove common markdown syntax so TTS reads cleanly."""
    text = re.sub(r"```[a-zA-Z0-9_-]*\n?", "", text)  # fenced code open
    text = re.sub(r"```", "", text)                      # fenced code close
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)  # headings
    text = re.sub(r"\*\*([^*]*)\*\*", r"\1", text)      # bold
    text = re.sub(r"\*([^*]*)\*", r"\1", text)           # italic
    text = re.sub(r"`([^`]*)`", r"\1", text)             # inline code
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text) # links
    text = re.sub(r"^\s*[-*]\s+", "", text, flags=re.MULTILINE)  # list items
    text = re.sub(r"\n{3,}", "\n\n", text)               # excess blank lines
    return text.strip()
