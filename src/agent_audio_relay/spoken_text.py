"""Sentence segmentation that preserves char offsets into the input.

Exists to support the tmux highlight feature: tts-popup needs to map mpv's
``time-pos`` onto a slice of the original `<stem>.txt` so it can drive
copy-mode selection in the caller pane. The popup will:

    text=$(tts-ctl spoken-text "$session")
    sentences=$(printf '%s' "$text" | aar-spoken-text)
    # each sentence line: <char_start>\t<char_end>\t<text>

The CLI emits TSV (not JSON) because the popup is bash and TSV survives
``IFS=$'\\t' read`` cleanly without jq. char_start/char_end are 0-based
half-open indices into the input string — slicing ``text[start:end]``
yields the sentence verbatim, including its trailing punctuation. Leading
whitespace between sentences is *not* included in any sentence's range, so
``end_n < start_{n+1}`` is normal.

The segmentation logic is the same as :mod:`agent_audio_relay.tts_stream`
(``.!?`` boundaries, abbreviation guard) but rewritten to track offsets
rather than build cleaned chunks.
"""

from __future__ import annotations

import sys
from typing import List, Tuple

from .tts_stream import _ABBREV, _SENTENCE_END_RE, _is_real_sentence_end


def segment_with_offsets(text: str) -> List[Tuple[int, int, str]]:
    """Split ``text`` into sentences keeping original-string indices.

    Returns a list of ``(char_start, char_end, sentence_text)`` triples
    where ``text[char_start:char_end] == sentence_text``. Trailing run of
    text without sentence-final punctuation is included as a final
    sentence so the whole input is always covered (modulo inter-sentence
    whitespace).
    """
    sentences: List[Tuple[int, int, str]] = []
    n = len(text)
    cursor = 0  # offset in original `text` we've consumed up to
    while cursor < n:
        # Skip leading whitespace so char_start lands on the first
        # printable char of the next sentence.
        while cursor < n and text[cursor].isspace():
            cursor += 1
        if cursor >= n:
            break
        # Skip over abbreviation-style ".`" matches without resetting
        # the search start, so "Dr. Smith arrived. He" finds the
        # *second* `.` as a real boundary instead of giving up at the
        # first one.
        scan = cursor
        boundary = None
        while True:
            m = _SENTENCE_END_RE.search(text, scan)
            if not m:
                break
            if _is_real_sentence_end(text, m.start()):
                boundary = m
                break
            scan = m.end()
        if boundary is not None:
            end = boundary.start() + 1
            sentences.append((cursor, end, text[cursor:end]))
            cursor = boundary.end()
            continue
        # No more sentence boundaries — emit the rest as a final
        # sentence, trimmed of trailing whitespace so end is tight.
        end = n
        while end > cursor and text[end - 1].isspace():
            end -= 1
        if end > cursor:
            sentences.append((cursor, end, text[cursor:end]))
        break
    return sentences


def main() -> int:
    """Read text on stdin, emit one TSV line per sentence on stdout.

    Format: ``<char_start>\\t<char_end>\\t<sentence_text>\\n``. Sentence
    text is emitted with whitespace runs (``\\n``, ``\\t``) collapsed to
    single spaces so a single TSV line still represents one sentence —
    consumers that need the verbatim sentence can re-slice the input
    using the offsets.
    """
    text = sys.stdin.read()
    for start, end, sentence in segment_with_offsets(text):
        flat = " ".join(sentence.split())
        sys.stdout.write(f"{start}\t{end}\t{flat}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
