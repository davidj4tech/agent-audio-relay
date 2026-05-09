#!/usr/bin/env python3
"""aar-clip-server — HTTP file server for finished TTS archive clips.

Serves files from a configured root (default
`~/.local/state/agent-audio-relay/`) so that any mpv — local or remote —
can `loadfile` them by URL instead of needing a local filesystem path.
This is the unification primitive that lets a single `mpv` backend drive
playback to any reachable mpv (cross-host via Tailscale, local via
shared filesystem) without per-clip scp.

Usage:
    aar-clip-server [--root DIR] [--port N] [--bind ADDR]

URL scheme:
    GET /<filename>            → serve file from ROOT/<filename>
    GET /clip/<filename>       → same; explicit /clip/ prefix accepted

Only files matching configured extensions are served (mp3/opus/ogg/wav)
and only direct children of ROOT (no `..` traversal, no subdirs). The
server is read-only; HEAD/GET only.

Companion to `aar-sink-stream` (live music) and `tts-stream` (live TTS).
All three follow the same shape: HTTP MP3 over the network so any mpv
can be the player.
"""

from __future__ import annotations

import argparse
import http.server
import os
import signal
import socket
import socketserver
import sys
import threading
from pathlib import Path

ALLOWED_EXTS = {".mp3", ".opus", ".ogg", ".wav"}

CONTENT_TYPES = {
    ".mp3": "audio/mpeg",
    ".opus": "audio/ogg",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
}


def _default_root() -> Path:
    state = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
    return state / "agent-audio-relay"


class ClipHandler(http.server.BaseHTTPRequestHandler):
    server_version = "aar-clip-server/1"
    root: Path = _default_root()

    def log_message(self, fmt, *args):
        # Quiet the default per-request access log; rely on systemd journal
        # for the lifecycle messages we emit ourselves.
        pass

    def _resolve(self, path: str) -> Path | None:
        # Strip query string + leading slash, accept optional /clip/ prefix.
        path = path.split("?", 1)[0].lstrip("/")
        if path.startswith("clip/"):
            path = path[len("clip/"):]
        if not path or "/" in path or path.startswith("."):
            return None
        candidate = self.root / path
        try:
            resolved = candidate.resolve()
        except OSError:
            return None
        # Resolved path must stay inside the root (defends against
        # symlink escapes — the watcher's archive dir is full of
        # `latest--…` symlinks pointing back into the same dir, but we
        # still don't want a symlink that points outside ROOT to leak
        # through).
        try:
            resolved.relative_to(self.root.resolve())
        except ValueError:
            return None
        if not resolved.is_file():
            return None
        if resolved.suffix.lower() not in ALLOWED_EXTS:
            return None
        return resolved

    def _send_404(self):
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()

    def _serve(self, write_body: bool):
        target = self._resolve(self.path)
        if target is None:
            self._send_404()
            return
        try:
            stat = target.stat()
        except OSError:
            self._send_404()
            return

        ctype = CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
        # Range support: parse a single "bytes=START-[END]" range so mpv
        # can seek without re-fetching from zero. For finished clips
        # this is safe — the file is complete, no live-stream replay
        # artifact like tts_stream had.
        rng = self.headers.get("Range")
        start = 0
        end = stat.st_size - 1
        partial = False
        if rng and rng.startswith("bytes="):
            spec = rng[len("bytes="):]
            try:
                s, _, e = spec.partition("-")
                if s:
                    start = int(s)
                if e:
                    end = int(e)
                if start < 0 or end >= stat.st_size or start > end:
                    raise ValueError
                partial = True
            except ValueError:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{stat.st_size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{stat.st_size}")
        self.end_headers()

        if not write_body:
            return

        try:
            with target.open("rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_HEAD(self):
        self._serve(write_body=False)

    def do_GET(self):
        self._serve(write_body=True)


class ClipServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> int:
    p = argparse.ArgumentParser(
        description="HTTP file server for AAR archive clips — feeds remote mpv via URL.",
    )
    p.add_argument("--root", default=os.environ.get("AAR_CLIP_ROOT"),
                   help="serve files from this directory "
                        "(default: $XDG_STATE_HOME/agent-audio-relay)")
    p.add_argument("--port", type=int,
                   default=int(os.environ.get("AAR_CLIP_PORT", "7773")))
    p.add_argument("--bind", default=os.environ.get("AAR_CLIP_BIND", "0.0.0.0"))
    args = p.parse_args()

    root = Path(args.root).expanduser() if args.root else _default_root()
    if not root.is_dir():
        print(f"aar-clip-server: root directory does not exist: {root}",
              file=sys.stderr)
        return 2

    handler_cls = type("Handler", (ClipHandler,), {"root": root})

    try:
        server = ClipServer((args.bind, args.port), handler_cls)
    except OSError as e:
        print(f"aar-clip-server: bind {args.bind}:{args.port} failed: {e}",
              file=sys.stderr)
        return 1

    host = socket.gethostname()
    print(f"aar-clip-server: serving {root} at http://{host}:{args.port}/<clip>",
          flush=True)

    stop_event = threading.Event()

    def _shutdown(*_):
        if stop_event.is_set():
            return
        stop_event.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
