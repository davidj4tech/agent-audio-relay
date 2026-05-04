"""tts-stream — incremental TTS for streaming model output.

Reads text on stdin, segments it incrementally on sentence boundaries,
renders each segment to audio in parallel (bounded), and dispatches the
clips to mpv in order via the voice channel's IPC socket — so audio
starts playing within ~1-2s of the first sentence completing instead
of waiting for the whole response to finish.

After the stream ends, the per-segment files are concatenated into a
single full-response clip and handed to ``tts-drop`` to be archived
with normal latest-symlink semantics — so replay/prev/next still walk
*responses*, not segments. Per-segment files are ephemeral and live in
``/tmp/tts-stream/<run-id>/``.

Usage:
    llm "explain X" | tts-stream [--engine edge|openai] [--voice NAME]
                                 [--socket PATH] [--tag llm] [--session ID]
                                 [--no-archive] [--max-workers N]

Why a sibling to tts-drop instead of an extension of it: tts-drop's
abstraction is "one clip → archive → latest". Stream segments aren't
independently replayable units — forcing a `--no-latest` flag onto
tts-drop just pushes the concept where it doesn't belong, and segments
would still pay the drop-dir/forwarder/scp/relay cost when streaming
needs to go straight to the local mpv socket. tts-stream owns its own
pipeline; tts-drop runs *once* at the end with the concatenated blob.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import List, Optional


# --- Segmenter -------------------------------------------------------------

# Sentence-ending punctuation followed by whitespace (or end of buffer).
# `(?<![A-Z])` in front of `.` would help with single-letter abbreviations
# but also breaks on legitimate sentence-final capital-letter words; skip.
_SENTENCE_END_RE = re.compile(r"([.!?])(\s+|$)")

# Abbreviations that end in `.` but don't terminate a sentence. Anything
# longer than this list is a wash — perfect segmentation isn't the goal,
# "good enough most of the time" is.
_ABBREV = {
    "mr.", "mrs.", "ms.", "dr.", "prof.", "sr.", "jr.", "st.",
    "vs.", "etc.", "e.g.", "i.e.", "no.", "vol.", "fig.", "inc.",
    "ltd.", "co.", "u.s.", "u.k.", "a.m.", "p.m.",
}

# Force a split when a chunk grows past this many chars without a sentence
# boundary, so a code-free wall of comma-separated text doesn't stall the
# whole pipeline. Splits at the last `,;:` + space inside the window.
_MAX_CHUNK = 240
_FORCE_SPLIT_RE = re.compile(r"[,;:]\s+")

# Fenced code blocks (``` ... ```). Stripped wholesale — there's no
# useful TTS rendering of code, and trying to read it character-by-
# character produces noise that competes with the actual response.
_CODE_FENCE_RE = re.compile(r"```[a-zA-Z0-9_+-]*\n.*?\n?```", re.DOTALL)


def _strip_code_blocks(text: str) -> str:
    return _CODE_FENCE_RE.sub("", text)


def _is_real_sentence_end(buf: str, pos: int) -> bool:
    """``buf[pos]`` is `.!?` — return whether it actually ends a sentence
    (i.e. isn't part of an abbreviation like ``Dr.``).
    """
    if buf[pos] != ".":
        return True
    # Walk backward to find the start of the current word.
    start = pos
    while start > 0 and buf[start - 1].isalpha():
        start -= 1
    word = buf[start : pos + 1].lower()
    return word not in _ABBREV


def _split_segments(buf: str, *, drain: bool) -> tuple[list[str], str]:
    """Pull complete segments out of ``buf``; return ``(segments, leftover)``.

    When ``drain=True`` (stream ended), the entire leftover is returned as
    a final segment regardless of whether it ended with sentence-final
    punctuation.
    """
    segments: list[str] = []
    while True:
        m = _SENTENCE_END_RE.search(buf)
        if m and _is_real_sentence_end(buf, m.start()):
            end = m.end()
            chunk = buf[:end].strip()
            if chunk:
                segments.append(chunk)
            buf = buf[end:]
            continue

        # No sentence boundary — but if we've accumulated too much, force
        # split at the latest soft boundary (`,;:`). Avoids a single
        # comma-separated wall holding up the whole stream.
        if len(buf) >= _MAX_CHUNK:
            soft_matches = list(_FORCE_SPLIT_RE.finditer(buf, 0, _MAX_CHUNK))
            if soft_matches:
                cut = soft_matches[-1].end()
                chunk = buf[:cut].strip()
                if chunk:
                    segments.append(chunk)
                buf = buf[cut:]
                continue
            # No soft boundary either — force split at MAX_CHUNK on a
            # whitespace boundary to avoid mid-word splits.
            ws = buf.rfind(" ", 0, _MAX_CHUNK)
            cut = ws + 1 if ws > _MAX_CHUNK // 2 else _MAX_CHUNK
            chunk = buf[:cut].strip()
            if chunk:
                segments.append(chunk)
            buf = buf[cut:]
            continue

        break

    if drain and buf.strip():
        segments.append(buf.strip())
        buf = ""
    return segments, buf


# --- Engines ---------------------------------------------------------------


def _render_edge(text: str, outfile: Path, *, voice: str, edge_bin: str) -> bool:
    proc = subprocess.run(
        [edge_bin, "--text", text, "--voice", voice, "--write-media", str(outfile)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc.returncode == 0 and outfile.exists() and outfile.stat().st_size > 0


def _render_openai(text: str, outfile: Path, *, voice: str, model: str, python_bin: str) -> bool:
    script = (
        "import os, sys\n"
        "from openai import OpenAI\n"
        "client = OpenAI()\n"
        "with client.audio.speech.with_streaming_response.create(\n"
        "    model=os.environ['TTS_MODEL'],\n"
        "    voice=os.environ['TTS_VOICE'],\n"
        "    input=os.environ['TTS_TEXT'],\n"
        ") as resp:\n"
        "    resp.stream_to_file(os.environ['TTS_OUTFILE'])\n"
    )
    env = os.environ.copy()
    env["TTS_MODEL"] = model
    env["TTS_VOICE"] = voice
    env["TTS_TEXT"] = text
    env["TTS_OUTFILE"] = str(outfile)
    proc = subprocess.run(
        [python_bin, "-c", script],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    return proc.returncode == 0 and outfile.exists() and outfile.stat().st_size > 0


# --- mpv IPC ---------------------------------------------------------------


def _mpv_send(socket_path: Path, command: list) -> Optional[dict]:
    """Send one mpv JSON-IPC command. Returns the parsed response or None."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(2.0)
            s.connect(str(socket_path))
            payload = json.dumps({"command": command}).encode() + b"\n"
            s.sendall(payload)
            buf = b""
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                try:
                    chunk = s.recv(4096)
                except socket.timeout:
                    break
                if not chunk:
                    break
                buf += chunk
                if b"\n" in buf:
                    break
        for line in buf.splitlines():
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if "error" in msg:
                return msg
        return None
    except OSError:
        return None


# --- Main pipeline ---------------------------------------------------------


class StreamRunner:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.run_id = uuid.uuid4().hex[:8]
        self.work_dir = Path(args.work_dir or f"/tmp/tts-stream/{self.run_id}")
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.executor = ThreadPoolExecutor(max_workers=args.max_workers)
        self.next_dispatch = 0
        self.dispatch_lock = threading.Lock()
        self.ready: dict[int, Path] = {}
        self.dispatched: list[Path] = []
        self.first_dispatch_done = threading.Event()
        self.errors: list[str] = []

    # --- segment rendering -------------------------------------------------

    def _render(self, seq: int, text: str) -> Optional[Path]:
        outfile = self.work_dir / f"{seq:04d}.mp3"
        ok = False
        if self.args.engine == "openai":
            ok = _render_openai(
                text, outfile,
                voice=self.args.voice or self.args.openai_voice,
                model=self.args.openai_model,
                python_bin=self.args.openai_python,
            )
            if not ok:  # fall back to edge
                self._log(f"seg {seq}: openai failed, falling back to edge")
                ok = _render_edge(
                    text, outfile,
                    voice=self.args.edge_voice, edge_bin=self.args.edge_bin,
                )
        else:
            ok = _render_edge(
                text, outfile,
                voice=self.args.voice or self.args.edge_voice,
                edge_bin=self.args.edge_bin,
            )
        if not ok:
            self.errors.append(f"render failed for segment {seq}")
            return None
        return outfile

    def _on_render_done(self, seq: int, fut: Future) -> None:
        try:
            path = fut.result()
        except Exception as e:  # noqa: BLE001
            self.errors.append(f"render exc seg {seq}: {e}")
            path = None
        with self.dispatch_lock:
            if path is not None:
                self.ready[seq] = path
            self._drain_locked()

    def _drain_locked(self) -> None:
        # Caller holds dispatch_lock.
        while self.next_dispatch in self.ready:
            seq = self.next_dispatch
            path = self.ready.pop(seq)
            self._dispatch_one(seq, path)
            self.dispatched.append(path)
            self.next_dispatch += 1

    def _dispatch_one(self, seq: int, path: Path) -> None:
        # First segment: replace whatever was loaded on voice channel and
        # unpause. Subsequent segments append-play so mpv's playlist
        # transitions between them gaplessly (within mp3 codec limits).
        mode = "replace" if seq == 0 else "append-play"
        resp = _mpv_send(self.args.socket, ["loadfile", str(path), mode])
        if resp and resp.get("error") and resp["error"] != "success":
            self.errors.append(f"loadfile seg {seq}: {resp.get('error')}")
            return
        if seq == 0:
            _mpv_send(self.args.socket, ["set_property", "pause", False])
            self.first_dispatch_done.set()
        self._log(f"seg {seq} → mpv ({mode})")

    # --- stream loop -------------------------------------------------------

    def run(self) -> int:
        if not self.args.socket.exists():
            print(
                f"tts-stream: voice socket not found: {self.args.socket}\n"
                "  start aar-mpv-tunnel on this host (or pass --socket).",
                file=sys.stderr,
            )
            return 2

        buf = ""
        seq = 0
        in_eof = False
        # Read stdin in modest chunks; flush segments as soon as they're
        # complete. Stdin is generally line-buffered when llm streams to a
        # pipe, but read1() handles partial reads cleanly in either case.
        # Strip code blocks lazily: we operate on a "speakable" view of
        # the buffer rather than the raw input, so we don't accidentally
        # treat code-internal periods as sentence boundaries.
        raw_buf = ""
        while not in_eof:
            chunk = sys.stdin.read(256)
            if not chunk:
                in_eof = True
            else:
                raw_buf += chunk
            # Re-strip on every iteration since a code fence may straddle
            # chunk boundaries; the operation is cheap on text this small.
            buf_view = _strip_code_blocks(raw_buf)
            if in_eof:
                # Drain remaining input as final segment regardless of
                # whether it ended on punctuation.
                segments, leftover = _split_segments(buf_view, drain=True)
            else:
                segments, leftover = _split_segments(buf_view, drain=False)
                # If we've stripped code blocks we can't easily reconcile
                # `leftover` back to a position in raw_buf. Simplest: only
                # advance raw_buf when a *complete* code block was the
                # only difference between raw_buf and buf_view (i.e. text
                # content matches), otherwise wait. This approximation:
                # consume from raw_buf the prefix that produced everything
                # before `leftover` in buf_view. Since stripping only
                # removes whole fenced blocks, we can splice raw_buf to
                # match by finding the suffix of buf_view in raw_buf.
                consumed = len(buf_view) - len(leftover)
                if consumed > 0:
                    # Find the position in raw_buf corresponding to
                    # `consumed` chars of buf_view by walking forward and
                    # skipping any code-block ranges.
                    raw_buf = _advance_raw(raw_buf, consumed)
            for chunk_text in segments:
                fut = self.executor.submit(self._render, seq, chunk_text)
                fut.add_done_callback(lambda f, s=seq: self._on_render_done(s, f))
                seq += 1

        # All segments queued. Wait for the executor to drain.
        self.executor.shutdown(wait=True)
        # One final dispatch pass in case the last render finished after
        # the executor's last on_done fired but before shutdown returned.
        with self.dispatch_lock:
            self._drain_locked()

        # Archive the full response as a single concatenated clip via
        # tts-drop, so replay/prev/next still walk *responses*.
        if not self.args.no_archive and self.dispatched:
            self._archive_concat()

        if self.errors:
            for e in self.errors:
                print(f"tts-stream: {e}", file=sys.stderr)
            return 1
        return 0

    # --- archive -----------------------------------------------------------

    def _archive_concat(self) -> None:
        """Drop a concatenated full-response clip into the watched dir.

        tts-drop expects *text* on stdin and re-renders — it has no
        from-file mode. Rather than re-render the whole response just for
        archive (doubles TTS cost), we drop the concatenated audio file
        directly into the watch dir with the standard stem format. The
        forwarder picks it up like any other clip; the relay's mpv
        backend on the phone writes the latest--<host>--<session>
        symlinks the same way it does for tts-drop emissions.

        Cheap concat: edge-tts and openai-tts both produce MP3s with
        consistent codec params, and mp3 is a frame-stream format that
        tolerates plain byte-concatenation. If quality issues appear
        later we can swap in `ffmpeg -f concat`.
        """
        drop_dir = Path(self.args.drop_dir)
        drop_dir.mkdir(parents=True, exist_ok=True)
        stem = _make_stem(self.args.tag, self.args.kind, self.args.session)
        # Stage outside the watched dir, then atomic rename in — same
        # pattern as tts-drop, so the watcher doesn't see partial writes.
        staging = drop_dir / f".{stem}.partial.mp3"
        final = drop_dir / f"{stem}.mp3"
        try:
            with staging.open("wb") as out:
                for p in self.dispatched:
                    out.write(p.read_bytes())
            staging.rename(final)
            self._log(f"archived {final}")
        except OSError as e:
            self.errors.append(f"archive write: {e}")
            staging.unlink(missing_ok=True)

    def _log(self, msg: str) -> None:
        if self.args.verbose:
            print(f"tts-stream[{self.run_id}]: {msg}", file=sys.stderr)


# --- stem (mirrors shell/hooks/lib/denote-stem.sh) ------------------------

_SLUG_RE = re.compile(r"[^A-Za-z0-9-]+")
_DASHES_RE = re.compile(r"-+")


def _slug(s: str) -> str:
    s = _SLUG_RE.sub("-", s)
    s = _DASHES_RE.sub("-", s)
    return s.strip("-")


def _make_stem(agent: str, kind: str, session_override: str = "") -> str:
    """Mirror of denote-stem.sh's ``make_stem``.

    Format: ``YYYYMMDDTHHMMSS--<host>--<session>__<persona>_<agent>_<kind>``

    The host segment is encoded by the *producer* (us) so backends on
    the playback host can disambiguate same-named sessions across hosts
    without hostname() lookups at archive time.
    """
    ts = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    host = _slug(socket.gethostname().split(".", 1)[0]) or "nohost"
    session = session_override
    if not session and os.environ.get("TMUX"):
        try:
            session = subprocess.check_output(
                ["tmux", "display-message", "-p", "#S"],
                stderr=subprocess.DEVNULL,
            ).decode().strip()
        except (OSError, subprocess.CalledProcessError):
            pass
    session = _slug(session) or "nosession"
    persona = _slug(os.environ.get("USER", "")) or "nopersona"
    agent_s = _slug(agent) or "noagent"
    kind_s = _slug(kind) or "nokind"
    return f"{ts}--{host}--{session}__{persona}_{agent_s}_{kind_s}"


def _advance_raw(raw_buf: str, target_speakable_len: int) -> str:
    """Return the suffix of ``raw_buf`` after consuming ``target_speakable_len``
    characters of speakable text (i.e. excluding stripped code blocks).
    """
    consumed = 0
    i = 0
    while i < len(raw_buf) and consumed < target_speakable_len:
        # Code fence start at this position?
        if raw_buf.startswith("```", i):
            end = raw_buf.find("```", i + 3)
            if end == -1:
                # Open fence with no close — wait for more input.
                return raw_buf[i:]
            i = end + 3
            continue
        i += 1
        consumed += 1
    return raw_buf[i:]


# --- CLI -------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tts-stream",
        description="Incremental TTS for streaming model output.",
    )
    p.add_argument("--engine", default=os.environ.get("RELAY_TTS_ENGINE", "edge"),
                   choices=["edge", "openai"])
    p.add_argument("--voice", default=None,
                   help="Engine-specific voice name (overrides per-engine default).")
    p.add_argument("--edge-voice",
                   default=os.environ.get("RELAY_EDGE_VOICE", "en-US-AriaNeural"))
    p.add_argument("--edge-bin",
                   default=os.environ.get("RELAY_EDGE_TTS_BIN", "edge-tts"))
    p.add_argument("--openai-voice",
                   default=os.environ.get("RELAY_OPENAI_VOICE", "marin"))
    p.add_argument("--openai-model",
                   default=os.environ.get("RELAY_OPENAI_MODEL", "gpt-4o-mini-tts"))
    p.add_argument("--openai-python",
                   default=os.environ.get("RELAY_OPENAI_PYTHON", "python3"))
    p.add_argument("--socket", type=Path,
                   default=Path(os.environ.get(
                       "AAR_VOICE_SOCKET",
                       str(Path(os.environ.get("XDG_STATE_HOME",
                                               Path.home() / ".local/state"))
                           / "agent-audio-relay" / "mpv-voice.sock"))),
                   help="mpv-voice IPC socket. Default: tunnel socket.")
    p.add_argument("--max-workers", type=int, default=2,
                   help="Bounded parallelism for TTS rendering.")
    p.add_argument("--tag", default="llm",
                   help="Agent tag for the archived full-response clip.")
    p.add_argument("--kind", default="stream",
                   help="Event kind for the archived clip.")
    p.add_argument("--session", default="",
                   help="Session ID for archive routing. Default: tmux session.")
    p.add_argument("--drop-dir",
                   default=os.environ.get("RELAY_LLM_DROP_DIR", "/tmp/tts-llm"),
                   help="Drop-dir for the final archived full-response clip.")
    p.add_argument("--no-archive", action="store_true",
                   help="Skip the post-stream archive (segments are still played).")
    p.add_argument("--work-dir", default=None,
                   help="Per-run scratch dir for segment files. Default: /tmp/tts-stream/<run-id>/")
    p.add_argument("--keep-work", action="store_true",
                   help="Don't remove the per-run scratch dir after the stream ends.")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main() -> None:
    args = _build_parser().parse_args()
    if not args.session:
        # Inherit tmux session if available — same convention as tts-drop.
        if os.environ.get("TMUX"):
            try:
                out = subprocess.check_output(
                    ["tmux", "display-message", "-p", "#S"],
                    stderr=subprocess.DEVNULL,
                ).decode().strip()
                args.session = out
            except (OSError, subprocess.CalledProcessError):
                pass

    runner = StreamRunner(args)
    rc = runner.run()
    if not args.keep_work:
        try:
            shutil.rmtree(runner.work_dir, ignore_errors=True)
        except OSError:
            pass
    sys.exit(rc)


if __name__ == "__main__":
    main()
