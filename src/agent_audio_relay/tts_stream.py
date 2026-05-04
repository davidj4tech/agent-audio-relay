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
import http.server
import json
import os
import queue
import re
import shutil
import socket
import socketserver
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


def _split_segments(buf: str, *, drain: bool, eager_first: bool = False) -> tuple[list[str], str]:
    """Pull complete segments out of ``buf``; return ``(segments, leftover)``.

    When ``drain=True`` (stream ended), the entire leftover is returned as
    a final segment regardless of whether it ended with sentence-final
    punctuation.

    When ``eager_first=True``, the *first* segment is allowed to cut at
    the first soft boundary (``,;:`` + space) or word-break past a low
    char threshold, without waiting for ``.!?``. This trades sentence-
    perfect prosody for time-to-first-audio: a typical opening clause
    can be playing within ~1-2s of the model emitting its first token,
    instead of waiting for the whole opening sentence to complete (which
    on a short llm response often coincides with the *end* of the
    response). Subsequent segments use the normal sentence-boundary
    rules so prosody stays natural after the first cut.
    """
    segments: list[str] = []
    eager_threshold = 60  # chars before we'll soft-split the first segment

    while True:
        m = _SENTENCE_END_RE.search(buf)
        if m and _is_real_sentence_end(buf, m.start()):
            end = m.end()
            chunk = buf[:end].strip()
            if chunk:
                segments.append(chunk)
            buf = buf[end:]
            continue

        # Eager first-segment split: if no segments emitted yet AND no
        # eager segment yet AND buf has enough content, cut at the first
        # soft boundary past the threshold. Prefer comma/semicolon/colon;
        # fall back to a word break if none exist.
        if eager_first and not segments and len(buf) >= eager_threshold:
            soft_matches = list(_FORCE_SPLIT_RE.finditer(buf, eager_threshold))
            if soft_matches:
                cut = soft_matches[0].end()
                chunk = buf[:cut].strip()
                if chunk:
                    segments.append(chunk)
                buf = buf[cut:]
                continue
            # No soft boundary visible yet — wait for more text rather
            # than splitting mid-clause. We'll fall through to the
            # MAX_CHUNK force-split if buf grows large.

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


def _render_openai(text: str, outfile: Path, *, voice: str, model: str, python_bin: str) -> tuple[bool, str]:
    """Returns (ok, stderr_text). Caller logs the stderr when ok is False
    so failures aren't silent — the most common failure mode is the
    chosen python lacking the ``openai`` module, which is recoverable
    by pointing ``--openai-python`` (or ``RELAY_OPENAI_PYTHON``) at the
    right venv.
    """
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
    err = proc.stderr.decode(errors="replace").strip()
    ok = proc.returncode == 0 and outfile.exists() and outfile.stat().st_size > 0
    return ok, err


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


# --- HTTP stream server ----------------------------------------------------
#
# Modeled on sam-radio: per-invocation HTTP endpoint that mpv (anywhere on
# the network) can `loadfile`. The producer (us) writes MP3 bytes into a
# queue; the HTTP handler dequeues and writes to the response. mpv handles
# buffering, network jitter, codec chunking — we don't.
#
# Why not per-segment loadfile-by-path: mpv-voice runs on the phone, the
# audio files exist on the producer host (homer/melr/sp4r). loadfile of a
# local path on the producer side resolves on the *phone's* filesystem
# where the file doesn't exist. HTTP fixes that without adding scp/sshfs.


class _StreamHandler(http.server.BaseHTTPRequestHandler):
    # Per-server queue + done-event + listener-close hook are wired in by
    # _start_stream_server.
    chunk_queue: "queue.Queue[Optional[bytes]]" = None  # type: ignore[assignment]
    handler_done: threading.Event = None  # type: ignore[assignment]
    close_listener = staticmethod(lambda: None)

    def log_message(self, fmt: str, *args) -> None:  # silence default access log
        pass

    def do_HEAD(self) -> None:
        # mpv/ffmpeg sometimes issues a HEAD probe before the playback
        # GET; answer it so the probe doesn't (a) drain the queue or
        # (b) confuse the demuxer about content type.
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()

    def do_GET(self) -> None:
        # Close the listener as soon as the first GET arrives — mpv (or
        # ffmpeg's HTTP demuxer underneath) sometimes opens a second
        # connection mid-playback to probe / range-read, which would
        # split the byte stream between two handlers and produce audio
        # corruption or apparent "looping" near the end. One-shot bind
        # avoids the whole class of problem.
        self.close_listener()
        # We accept any path — the URL we hand to mpv is the only one we
        # advertise, and there's no security boundary worth enforcing here
        # (the server only runs for the lifetime of one tts-stream call,
        # bound to localhost-or-Tailscale by the operator's network config).
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        # No Content-Length since we don't know it ahead of time. mpv
        # tolerates a connection-close EOF as the natural stream end.
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            while True:
                chunk = self.chunk_queue.get()
                if chunk is None:  # sentinel: stream complete
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # Client (mpv) closed early. Drain any remaining chunks so the
            # producer doesn't block on a full queue forever.
            while True:
                try:
                    if self.chunk_queue.get_nowait() is None:
                        break
                except queue.Empty:
                    break
        finally:
            # Signal the producer that the response was fully flushed
            # (or the client gave up). Caller waits on this before tearing
            # down the server / exiting the process — without it the
            # daemon-thread handler can be killed mid-write when main
            # returns, truncating mpv's audio.
            self.handler_done.set()


class _StreamServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _start_stream_server(
    host: str = "0.0.0.0",
) -> tuple[_StreamServer, "queue.Queue[Optional[bytes]]", int, threading.Event]:
    """Bind a server on a random port, return (server, chunk_queue, port,
    handler_done). The server runs in a daemon thread; caller pushes
    bytes into the queue, then ``None`` as end-of-stream sentinel, then
    waits on ``handler_done`` before tearing the server down.
    """
    chunk_q: "queue.Queue[Optional[bytes]]" = queue.Queue(maxsize=64)
    handler_done = threading.Event()
    listener_closed = threading.Event()

    # Holder so the handler can close the listener via close_listener()
    # without circular-importing the server.
    server_ref: list = []

    def close_listener() -> None:
        if listener_closed.is_set():
            return
        listener_closed.set()
        srv = server_ref[0] if server_ref else None
        if srv is not None:
            try:
                # Stop accepting new connections without disturbing the
                # in-flight request. server_close() closes the listening
                # socket; existing handler threads keep running.
                srv.server_close()
            except OSError:
                pass

    handler_cls = type(
        "BoundStreamHandler",
        (_StreamHandler,),
        {
            "chunk_queue": chunk_q,
            "handler_done": handler_done,
            "close_listener": staticmethod(close_listener),
        },
    )
    server = _StreamServer((host, 0), handler_cls)
    server_ref.append(server)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, chunk_q, port, handler_done


def _resolve_advertise_host(target_socket_path: Optional[Path]) -> str:
    """Pick a hostname/IP that the playback host (typically the phone) can
    reach back to us on. Strategy:
      1. Explicit AAR_STREAM_HOST env override (best — operator knows).
      2. socket.gethostname() — works when Tailscale MagicDNS or LAN DNS
         resolves the hostname phone-side.
      3. Fallback: a UDP "connect" to a public IP to discover the local
         outbound interface IP (no packets sent).
    Pick 1 wins outright. Pick 2/3 are best-effort guesses.
    """
    explicit = os.environ.get("AAR_STREAM_HOST")
    if explicit:
        return explicit
    name = socket.gethostname()
    if name and name != "localhost":
        return name
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


# --- Main pipeline ---------------------------------------------------------


class StreamRunner:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.run_id = uuid.uuid4().hex[:8]
        self.work_dir = Path(args.work_dir or f"/tmp/tts-stream/{self.run_id}")
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.executor = ThreadPoolExecutor(max_workers=args.max_workers)
        self._t0 = time.monotonic()
        self.next_dispatch = 0
        self.dispatch_lock = threading.Lock()
        self.ready: dict[int, Path] = {}
        self.dispatched: list[Path] = []
        self.first_dispatch_done = threading.Event()
        self.errors: list[str] = []
        self._stream_server: Optional[_StreamServer] = None
        self._stream_queue: Optional["queue.Queue[Optional[bytes]]"] = None
        self._handler_done: Optional[threading.Event] = None

    # --- segment rendering -------------------------------------------------

    def _render(self, seq: int, text: str) -> Optional[Path]:
        outfile = self.work_dir / f"{seq:04d}.mp3"
        ok = False
        if self.args.engine == "openai":
            ok, err = _render_openai(
                text, outfile,
                voice=self.args.voice or self.args.openai_voice,
                model=self.args.openai_model,
                python_bin=self.args.openai_python,
            )
            if not ok:  # fall back to edge
                # Surface the openai stderr so the user can fix the
                # underlying issue (most common: ``ModuleNotFoundError:
                # No module named 'openai'`` because RELAY_OPENAI_PYTHON
                # points at a python without the openai package).
                self.errors.append(
                    f"seg {seq}: openai failed ({err or 'no stderr'}); "
                    f"falling back to edge"
                )
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
        # Stream the segment's MP3 bytes into the HTTP stream queue. mpv
        # is connected to the HTTP endpoint (set up at run start) and
        # consumes the bytes as they arrive, so playback proceeds in
        # order without any per-segment loadfile cost. Concatenated MP3
        # frames produce a valid stream; mpv handles framing.
        try:
            data = path.read_bytes()
        except OSError as e:
            self.errors.append(f"read seg {seq}: {e}")
            return
        if self._stream_queue is None:
            self.errors.append(f"seg {seq}: stream server not running")
            return
        self._stream_queue.put(data)
        if seq == 0:
            self.first_dispatch_done.set()
        self._log(f"seg {seq} → stream ({len(data)} bytes)")

    # --- stream loop -------------------------------------------------------

    def run(self) -> int:
        if not self.args.socket.exists():
            print(
                f"tts-stream: voice socket not found: {self.args.socket}\n"
                "  start aar-mpv-tunnel on this host (or pass --socket).",
                file=sys.stderr,
            )
            return 2

        # Start the HTTP stream server, tell mpv to connect to it. mpv
        # holds the connection open and consumes MP3 bytes as we push
        # them; we close the stream (sentinel) when all segments have
        # been dispatched, mpv then plays through its remaining buffer
        # and goes idle.
        self._stream_server, self._stream_queue, port, self._handler_done = _start_stream_server()
        host = _resolve_advertise_host(self.args.socket)
        url = f"http://{host}:{port}/stream.mp3"
        self._log(f"stream URL = {url}")
        # Hard-clear any prior playback state on the voice channel
        # before loading our URL. Without this, content from a previous
        # tts-stream run that was paused mid-cache (or never fully
        # consumed) can replay alongside / after the new content — the
        # user hears a leftover snippet from an earlier response.
        # `stop` clears playlist + flushes demuxer cache + halts
        # playback; `loadfile replace` then starts genuinely fresh.
        _mpv_send(self.args.socket, ["stop"])
        load_resp = _mpv_send(self.args.socket, ["loadfile", url, "replace"])
        if load_resp and load_resp.get("error") and load_resp["error"] != "success":
            self.errors.append(f"mpv loadfile {url}: {load_resp.get('error')}")
            return 1
        _mpv_send(self.args.socket, ["set_property", "pause", False])

        buf = ""
        seq = 0
        in_eof = False
        # CRITICAL: read stdin via os.read(0, ...) instead of sys.stdin.read().
        # Python's sys.stdin is wrapped in a TextIOWrapper that block-buffers
        # (~8KB) when stdin is a pipe — so sys.stdin.read(N) waits for 8KB
        # or EOF, defeating streaming entirely (a typical llm response is
        # well under 8KB; you'd see nothing until the model finished). Raw
        # os.read on fd 0 returns as soon as the kernel has any bytes for
        # us, which is what streaming needs.
        # Strip code blocks lazily: we operate on a "speakable" view of
        # the buffer rather than the raw input, so we don't accidentally
        # treat code-internal periods as sentence boundaries.
        raw_buf = ""
        while not in_eof:
            try:
                raw = os.read(0, 4096)
            except OSError:
                raw = b""
            if not raw:
                in_eof = True
            else:
                chunk = raw.decode("utf-8", errors="replace")
                raw_buf += chunk
                if self.args.tee:
                    # Echo through unbuffered so the user sees text appear at
                    # the same cadence as it streams through us — same UX as
                    # `llm "..."` without tts-stream in the pipe.
                    # Swallow BrokenPipeError so an early-closing downstream
                    # consumer (e.g. `tee >(tts-stream >/dev/null)` where
                    # tts-stream exits before llm finishes) doesn't trash
                    # the user's terminal with a stack trace.
                    try:
                        sys.stdout.write(chunk)
                        sys.stdout.flush()
                    except BrokenPipeError:
                        # Drop to no-op tee for the rest of the run.
                        self.args.tee = False
            # Re-strip on every iteration since a code fence may straddle
            # chunk boundaries; the operation is cheap on text this small.
            buf_view = _strip_code_blocks(raw_buf)
            # Eager-first-segment is gated on "we haven't dispatched
            # anything yet for this run" — once seg 0 is out the door,
            # downstream segments use normal sentence-boundary rules so
            # prosody stays clean.
            eager = (seq == 0)
            if in_eof:
                # Drain remaining input as final segment regardless of
                # whether it ended on punctuation.
                segments, leftover = _split_segments(buf_view, drain=True, eager_first=eager)
            else:
                segments, leftover = _split_segments(buf_view, drain=False, eager_first=eager)
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
                self._log(f"seg {seq} cut ({len(chunk_text)} chars)")
                fut = self.executor.submit(self._render, seq, chunk_text)
                fut.add_done_callback(lambda f, s=seq: self._on_render_done(s, f))
                seq += 1

        # All segments queued. Wait for the executor to drain.
        self.executor.shutdown(wait=True)
        # One final dispatch pass in case the last render finished after
        # the executor's last on_done fired but before shutdown returned.
        with self.dispatch_lock:
            self._drain_locked()

        # Signal end-of-stream to the HTTP handler. mpv reads up to its
        # buffer and then plays it out; when its buffer empties it goes
        # idle. We don't wait for mpv to finish playing — the user's
        # shell would feel stuck for the full audio duration. Instead we
        # exit; mpv keeps playing what it's already buffered (which
        # should be everything, since the stream completes faster than
        # playback for typical responses).
        if self._stream_queue is not None:
            self._stream_queue.put(None)
        # Wait for the HTTP handler to fully flush all chunks to mpv
        # before tearing down the server. Without this, the daemon
        # handler thread can be killed mid-write when main exits and
        # mpv loses the tail of the audio.
        if self._handler_done is not None:
            self._handler_done.wait(timeout=30)
        if self._stream_server is not None:
            # server_close was likely already called by close_listener()
            # on first GET, but call shutdown() to stop the serve_forever
            # loop (idempotent).
            self._stream_server.shutdown()
        self._log("stream closed")

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
            elapsed = time.monotonic() - self._t0
            print(f"tts-stream[{self.run_id}] +{elapsed:5.2f}s: {msg}",
                  file=sys.stderr)


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
    # Default engine: respect explicit RELAY_TTS_ENGINE; otherwise prefer
    # openai when OPENAI_API_KEY is present (it sounds noticeably better
    # for long-form spoken content), and fall back to edge so the tool
    # works out of the box without an API key.
    _default_engine = os.environ.get("RELAY_TTS_ENGINE") or (
        "openai" if os.environ.get("OPENAI_API_KEY") else "edge"
    )
    p.add_argument("--engine", default=_default_engine,
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
    p.add_argument("--no-tee", dest="tee", action="store_false",
                   help="Don't echo stdin to stdout. Default: tee everything "
                        "through so the user sees the text in their terminal.")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(tee=True)
    return p


def _autodetect_openai_python(current: str) -> str:
    """If ``current`` doesn't have the ``openai`` module, look for one that
    does in the usual pipx venv locations. Returns the first python that
    can ``import openai`` (current preferred), else ``current`` unchanged
    so the caller's eventual error message stays attributable.
    """
    candidates = [current]
    pipx_root = Path(os.environ.get("PIPX_HOME",
                                    Path.home() / ".local" / "pipx"))
    candidates.append(str(pipx_root / "venvs" / "openai" / "bin" / "python3"))
    candidates.append(str(pipx_root / "venvs" / "llm" / "bin" / "python3"))
    for c in candidates:
        if not c or not Path(c).exists() and c != "python3":
            continue
        try:
            r = subprocess.run([c, "-c", "import openai"],
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL,
                               timeout=2)
            if r.returncode == 0:
                return c
        except (OSError, subprocess.SubprocessError):
            continue
    return current


def main() -> None:
    args = _build_parser().parse_args()
    if args.engine == "openai":
        args.openai_python = _autodetect_openai_python(args.openai_python)
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
    try:
        rc = runner.run()
    except BrokenPipeError:
        # Final safety net: any flush at interpreter shutdown that hits
        # a closed downstream pipe shouldn't print a traceback.
        rc = 0
    if not args.keep_work:
        try:
            shutil.rmtree(runner.work_dir, ignore_errors=True)
        except OSError:
            pass
    # Avoid the "Exception ignored on flushing sys.stdout" message Python
    # prints at shutdown when stdout is a broken pipe — close stdout so
    # the interpreter doesn't try to flush it.
    try:
        sys.stdout.close()
    except (BrokenPipeError, OSError):
        pass
    sys.exit(rc)


if __name__ == "__main__":
    main()
