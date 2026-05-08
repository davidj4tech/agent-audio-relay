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
    res = _pactl(
        "load-module", "module-null-sink",
        f"sink_name={name}",
        "sink_properties=device.description=AAR-Sink",
    )
    if res.returncode != 0:
        raise RuntimeError(f"failed to create null-sink: {res.stderr.strip()}")
    return res.stdout.strip()


def unload_module(module_id):
    if module_id:
        _pactl("unload-module", module_id)


class Encoder:
    """ffmpeg pipeline reading the sink monitor and producing MP3 bytes
    on stdout. Bytes are fanned out to subscribed Queue consumers; a
    consumer whose queue fills (slow client) is dropped so the producer
    can never stall on one bad listener.
    """

    def __init__(self, sink_name, bitrate):
        self.sink_name = sink_name
        self.bitrate = bitrate
        self.proc = None
        self.subscribers = []
        self.lock = threading.Lock()

    def start(self):
        cmd = [
            "ffmpeg",
            "-loglevel", "warning",
            "-f", "pulse",
            "-i", f"{self.sink_name}.monitor",
            "-c:a", "libmp3lame",
            "-b:a", self.bitrate,
            "-f", "mp3",
            "-",
        ]
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=sys.stderr, bufsize=0,
        )
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        try:
            while True:
                chunk = self.proc.stdout.read(4096)
                if not chunk:
                    break
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
        q = queue.Queue(maxsize=128)
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


class StreamHandler(http.server.BaseHTTPRequestHandler):
    encoder = None  # bound per-server class

    def log_message(self, fmt, *args):
        pass

    def _headers(self):
        self.send_header("Content-Type", "audio/mpeg")
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
    p.add_argument("--bitrate", default=os.environ.get("AAR_SINK_BITRATE", "192k"))
    args = p.parse_args()

    try:
        module_id = ensure_sink(args.sink)
    except RuntimeError as e:
        print(f"aar-sink-stream: {e}", file=sys.stderr)
        return 2

    encoder = Encoder(args.sink, args.bitrate)
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
    url = f"http://{host}:{args.port}/sink.mp3"
    print(f"aar-sink-stream: sink='{args.sink}', stream={url}", flush=True)

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
