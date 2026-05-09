#!/usr/bin/env python3
"""aar-sink-stream — virtual PipeWire/PulseAudio sink that streams to mpv.

Creates (or reuses) a null-sink, reads its monitor through ffmpeg as
encoded MP3, and serves the stream over HTTP for mpv (anywhere on the
network) to loadfile.

Usage:
    aar-sink-stream [--sink NAME] [--port N] [--bind ADDR] [--bitrate B]

Routing audio in:
    all apps:       pactl set-default-sink aar
    one stream:     pactl move-sink-input <id> aar
    per command:    PULSE_SINK=aar mpv ./song.mp3   (or aar-sink-run)

Attaching a player:
    aar-sink-connect <host>      # tells mpv-music to loadfile the URL
"""

from __future__ import annotations

import argparse
import http.server
import os
import queue
import signal
import socket
import socketserver
import subprocess
import sys
import threading


def _pactl(*args, capture=True):
    try:
        return subprocess.run(
            ["pactl", *args],
            capture_output=capture, text=True
        )
    except FileNotFoundError as e:
        raise RuntimeError(
            "pactl not found on PATH. "
            "On Fedora/RHEL: dnf install pulseaudio-utils pipewire-pulseaudio. "
            "On Debian/Ubuntu: apt install pulseaudio-utils pipewire-pulse."
        ) from e


def ensure_sink(name):
    """Ensure a null-sink with the given name exists. Returns the module
    ID we loaded (None if it pre-existed) so the caller can unload on
    exit and not leave a sink lingering between runs.
    """
    out = _pactl("list", "short", "sinks")
    if out.returncode != 0:
        raise RuntimeError(
            f"pactl unavailable ({out.stderr.strip() or 'no PulseAudio shim?'}). "
            "On Fedora/RHEL: dnf install pulseaudio-utils pipewire-pulseaudio"
        )
    for line in out.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) > 1 and parts[1] == name:
            return None  # already exists; we don't own it
    # Derive a human-readable description from the sink name. Without
    # this, every null-sink showed up in pavucontrol as the same
    # "AAR-Sink" label, indistinguishable in the device picker once you
    # had more than one (e.g. `aar` + `aar-music`).
    if name == "aar":
        label = "general"
    elif name.startswith("aar-"):
        label = name[4:]
    else:
        label = name
    res = _pactl(
        "load-module", "module-null-sink",
        f"sink_name={name}",
        f"sink_properties=device.description=AAR-{label}",
    )
    if res.returncode != 0:
        raise RuntimeError(f"failed to create null-sink: {res.stderr.strip()}")
    return res.stdout.strip()


def unload_module(module_id):
    if module_id:
        _pactl("unload-module", module_id)


def _scan_ogg_setup(buf: bytes, page_count: int) -> tuple[int, int] | None:
    """Walk Ogg pages from the start of `buf`, returning (bytes_through_n_pages,
    pages_seen) once `page_count` pages have been fully buffered, else None.

    For Ogg-Opus, the first two pages carry the codec setup (OpusHead,
    OpusTags). New HTTP subscribers connecting mid-stream miss those if
    we don't replay them, and Opus-in-Ogg requires them — without them
    every consumer reports `[ffmpeg/demuxer] ogg: Codec not found`. By
    capturing the first N pages once and prepending those bytes to each
    new subscriber's queue, mid-stream connection works the same as a
    fresh decoder seeing the bitstream from byte zero.

    Page format reference: each page starts with the magic `OggS`,
    a 23-byte fixed header, a 1-byte segment count, that many segment
    lengths, and finally the segment payloads (whose total length is the
    sum of the segment table). So a page is 27 + n_segments +
    sum(segment_table) bytes long.
    """
    pos = 0
    seen = 0
    while seen < page_count:
        if pos + 27 > len(buf):
            return None
        if buf[pos:pos + 4] != b"OggS":
            return None  # not Ogg, or stream desynced; caller should give up
        n_segments = buf[pos + 26]
        header_end = pos + 27 + n_segments
        if header_end > len(buf):
            return None
        payload_len = sum(buf[pos + 27:header_end])
        page_end = header_end + payload_len
        if page_end > len(buf):
            return None
        pos = page_end
        seen += 1
    return pos, seen


class Encoder:
    """ffmpeg pipeline reading the sink monitor and producing encoded
    audio bytes on stdout. Bytes are fanned out to subscribed Queue
    consumers; a consumer whose queue fills (slow client) is dropped so
    the producer can never stall on one bad listener.

    Default codec is Opus in an Ogg container with 10 ms frames and
    `-application lowdelay` — gives ~10 ms encoder latency vs ~50-100 ms
    for MP3, with similar perceived quality at half the bitrate. mpv
    decodes Opus natively.

    For Opus, the first two Ogg pages (OpusHead, OpusTags) are cached
    via `_scan_ogg_setup` and prepended to every new subscriber so a
    mid-stream connection still has the codec metadata it needs. MP3
    has no equivalent stream-level setup so this fast-path is skipped.
    """

    # Number of leading Ogg pages that contain Opus codec setup. OpusHead
    # is page 0 (BOS), OpusTags is page 1 — together they're a few hundred
    # bytes and need to land at the start of every subscriber's stream.
    _OPUS_SETUP_PAGES = 2

    def __init__(self, sink_name, bitrate, codec="opus"):
        self.sink_name = sink_name
        self.bitrate = bitrate
        self.codec = codec
        self.proc = None
        self.subscribers = []
        self.lock = threading.Lock()
        # Codec setup bytes captured from the start of the ffmpeg stream;
        # prepended to each new subscriber's queue (Opus only — see class
        # docstring). `None` until enough pages have arrived; an empty
        # bytes value means we gave up (e.g. stream desync) and new
        # subscribers won't get a setup prefix.
        self._setup_bytes: bytes | None = None
        self._setup_buf = bytearray()
        self._setup_done = False

    def start(self):
        # Plain pulse → encoder pipeline. Tried `-flush_packets 1`,
        # `-fflags nobuffer`, and `-fragment_size 1024` to tighten
        # producer-side buffering for pause/resume responsiveness, but
        # they wedged ffmpeg's output for our stream (no bytes delivered
        # past the codec setup pages). The visible end-to-end latency is
        # dominated by client-side TCP RCV buffering anyway, not by
        # producer-side muxer holdback, so the right place to push
        # further is the consumer / network layer, not here.
        common_input = [
            "ffmpeg",
            "-loglevel", "warning",
            "-f", "pulse",
            "-i", f"{self.sink_name}.monitor",
        ]
        if self.codec == "opus":
            cmd = common_input + [
                "-c:a", "libopus",
                "-b:a", self.bitrate,
                "-application", "lowdelay",
                "-frame_duration", "10",
                "-f", "ogg",
                "-",
            ]
        elif self.codec == "mp3":
            cmd = common_input + [
                "-c:a", "libmp3lame",
                "-b:a", self.bitrate,
                "-f", "mp3",
                "-",
            ]
        else:
            raise ValueError(f"unknown codec: {self.codec}")
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=sys.stderr, bufsize=0,
        )
        threading.Thread(target=self._reader, daemon=True).start()

    def _capture_setup(self, chunk: bytes) -> None:
        """For Opus, accumulate ffmpeg output until the first N Ogg pages
        are buffered, then snapshot them as `_setup_bytes` for replay to
        new subscribers. No-op once setup is done or for non-Opus codecs.
        """
        if self._setup_done or self.codec != "opus":
            return
        self._setup_buf.extend(chunk)
        result = _scan_ogg_setup(bytes(self._setup_buf), self._OPUS_SETUP_PAGES)
        if result is None:
            # Need more bytes (or stream isn't Ogg — bail if we've buffered
            # well past where headers should be without finding pages).
            if len(self._setup_buf) > 64 * 1024:
                self._setup_bytes = b""
                self._setup_done = True
                self._setup_buf.clear()
            return
        consumed, _ = result
        self._setup_bytes = bytes(self._setup_buf[:consumed])
        self._setup_done = True
        self._setup_buf.clear()

    def _reader(self):
        try:
            while True:
                chunk = self.proc.stdout.read(4096)
                if not chunk:
                    break
                if not self._setup_done:
                    self._capture_setup(chunk)
                with self.lock:
                    for q in list(self.subscribers):
                        try:
                            q.put_nowait(chunk)
                        except queue.Full:
                            # Slow consumer; evict.
                            self.subscribers.remove(q)
                            try:
                                q.put_nowait(None)
                            except queue.Full:
                                pass
        finally:
            with self.lock:
                for q in self.subscribers:
                    try:
                        q.put_nowait(None)
                    except queue.Full:
                        pass
                self.subscribers.clear()

    def subscribe(self):
        # Tight queue: each chunk is ~4 KB of encoded audio (~250 ms at
        # 128 kbps Opus), so maxsize=8 caps producer→consumer buffering
        # at ~2 s. Was 128 (~32 s) — enough that brief consumer hiccups
        # let the queue fill and then the buffered audio took ~10 s to
        # drain through the player after the source paused. Smaller
        # queue means a stuck consumer is evicted earlier rather than
        # silently growing latency for everyone.
        q = queue.Queue(maxsize=8)
        # Prepend codec setup bytes (Opus only) so a mid-stream subscriber
        # sees the OpusHead/OpusTags pages before live audio. Done before
        # the queue joins `self.subscribers`, so the setup bytes can't
        # interleave with a live chunk that arrives concurrently — by the
        # time the producer can hand a chunk to this queue, the setup is
        # already at the head of it.
        if self._setup_bytes:
            try:
                q.put_nowait(self._setup_bytes)
            except queue.Full:
                pass
        with self.lock:
            self.subscribers.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            try:
                self.subscribers.remove(q)
            except ValueError:
                pass

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()


CONTENT_TYPES = {"opus": "audio/ogg", "mp3": "audio/mpeg"}


class StreamHandler(http.server.BaseHTTPRequestHandler):
    encoder = None  # bound per-server class

    def log_message(self, fmt, *args):
        pass

    def _headers(self):
        self.send_header("Content-Type",
                         CONTENT_TYPES.get(self.encoder.codec, "audio/mpeg"))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        # Deliberately no Accept-Ranges — same reasoning as tts_stream:
        # mpv otherwise speculatively seeks back to zero on the MP3
        # header probe and replays the opening bytes.
        self.end_headers()

    def do_HEAD(self):
        self.send_response(200)
        self._headers()

    def do_GET(self):
        self.send_response(200)
        self._headers()
        q = self.encoder.subscribe()
        try:
            while True:
                chunk = q.get()
                if chunk is None:
                    return
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.encoder.unsubscribe(q)


class StreamServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    p = argparse.ArgumentParser(
        description="AAR null-sink HTTP stream — route any PipeWire app to mpv.",
    )
    p.add_argument("--sink", default=os.environ.get("AAR_SINK", "aar"))
    p.add_argument("--port", type=int,
                   default=int(os.environ.get("AAR_SINK_PORT", "7771")))
    p.add_argument("--bind", default=os.environ.get("AAR_SINK_BIND", "0.0.0.0"))
    p.add_argument("--bitrate", default=os.environ.get("AAR_SINK_BITRATE", "128k"),
                   help="encoder bitrate (default: 128k — Opus is roughly twice as efficient as MP3)")
    p.add_argument("--codec", default=os.environ.get("AAR_SINK_CODEC", "opus"),
                   choices=["opus", "mp3"],
                   help="encoder codec (default: opus — ~10ms encoder latency vs MP3's ~50-100ms)")
    args = p.parse_args()

    try:
        module_id = ensure_sink(args.sink)
    except RuntimeError as e:
        print(f"aar-sink-stream: {e}", file=sys.stderr)
        return 2

    encoder = Encoder(args.sink, args.bitrate, codec=args.codec)
    encoder.start()

    handler_cls = type("Handler", (StreamHandler,), {"encoder": encoder})
    try:
        server = StreamServer((args.bind, args.port), handler_cls)
    except OSError as e:
        encoder.stop()
        unload_module(module_id)
        print(f"aar-sink-stream: bind {args.bind}:{args.port} failed: {e}",
              file=sys.stderr)
        return 1

    host = socket.gethostname()
    ext = "opus" if args.codec == "opus" else "mp3"
    # Path is cosmetic — the server returns the live encoder stream
    # regardless of path — but we expose a readable URL for logs and
    # for consumers that pick up content type from the suffix.
    url = f"http://{host}:{args.port}/sink.{ext}"
    print(f"aar-sink-stream: sink='{args.sink}', codec={args.codec}, stream={url}",
          flush=True)

    stop_event = threading.Event()

    def _shutdown(*_):
        if stop_event.is_set():
            return
        stop_event.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        server.serve_forever()
    finally:
        encoder.stop()
        unload_module(module_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
