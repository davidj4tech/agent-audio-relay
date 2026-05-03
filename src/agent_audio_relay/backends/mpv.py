"""mpv playback backend.

Plays audio locally (or on a remote mpv instance) using mpv. Supports both
direct invocation and IPC via an existing mpv socket.

Environment variables:
    RELAY_MPV_BIN           Path to mpv binary (default: mpv)
    RELAY_MPV_SOCKET        IPC socket path — if set, sends commands to a
                            running mpv instance instead of spawning a new one
    RELAY_MPV_ARGS          Extra mpv arguments, space-separated (default: "")
    RELAY_MPV_WAIT          Wait for playback to finish before returning (default: 1)
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from pathlib import Path

from .base import PlaybackBackend, original_name


class MpvBackend(PlaybackBackend):
    name = "mpv"

    def __init__(self, target: str | None = None) -> None:
        self.bin = os.environ.get("RELAY_MPV_BIN", "mpv")
        self.ipc_socket = os.environ.get("RELAY_MPV_SOCKET", "")
        self.extra_args = os.environ.get("RELAY_MPV_ARGS", "").split() if os.environ.get("RELAY_MPV_ARGS") else []
        self.wait = os.environ.get("RELAY_MPV_WAIT", "1") == "1"
        self.target = target
        self._proc: subprocess.Popen | None = None

        if target and not any(a.startswith("--audio-device") for a in self.extra_args):
            device = target if "/" in target else f"pulse/{target}"
            self.extra_args = [*self.extra_args, f"--audio-device={device}"]

    def _send_ipc(self, command: list) -> dict | None:
        """Send a JSON IPC command to a running mpv instance."""
        if not self.ipc_socket:
            return None
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(5)
            sock.connect(self.ipc_socket)
            msg = json.dumps({"command": command}) + "\n"
            sock.sendall(msg.encode())
            data = sock.recv(4096)
            sock.close()
            return json.loads(data.decode())
        except (OSError, json.JSONDecodeError):
            return None

    def wait_for_playback(self) -> None:
        # If using IPC, poll mpv for idle state
        if self.ipc_socket:
            for _ in range(120):
                resp = self._send_ipc(["get_property", "idle-active"])
                if resp and resp.get("data") is True:
                    return
                time.sleep(1)
            return

        # If we spawned a process, wait for it
        if self._proc is not None:
            try:
                self._proc.wait(timeout=120)
            except subprocess.TimeoutExpired:
                pass
            self._proc = None

    def _update_latest(self, path: Path) -> Path:
        state_root = Path(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")))
        state = state_root / "agent-audio-relay"
        state.mkdir(parents=True, exist_ok=True)
        archive = state / original_name(path)
        try:
            if archive.resolve() != path.resolve():
                import shutil as _shutil
                _shutil.copy2(str(path), str(archive))
        except OSError:
            return path
        # Build pointer set: global, per-session, host-namespaced,
        # per-session__agent. Mirrors the layout ssh_termux writes so
        # `tts-ctl replay_target` can resolve session-scoped audio
        # regardless of which backend produced the clip.
        #
        # Stem (current):  <ts>--<host>--<session>__<persona>_<agent>_<kind>
        # Stem (legacy):   <ts>--<session>__<persona>_<agent>_<kind>
        # The host is parsed *from the stem* (the producer's hostname),
        # not from socket.gethostname() at archive time (which would be
        # the relay host — useless for cross-host disambiguation).
        ext = path.suffix
        links = [state / f"latest{ext}"]
        stem = archive.stem
        host_in_stem = ""
        session = ""
        agent = ""
        if "--" in stem and "__" in stem:
            try:
                after_ts = stem.split("--", 1)[1]
                left, rest = after_ts.split("__", 1)
                if "--" in left:
                    host_in_stem, session = left.split("--", 1)
                else:
                    session = left
                parts = rest.split("_")
                if len(parts) >= 3:
                    agent = parts[-2]
            except ValueError:
                session = ""
        if session:
            if host_in_stem:
                links.append(state / f"latest--{host_in_stem}--{session}{ext}")
            links.append(state / f"latest--{session}{ext}")
            if agent:
                links.append(state / f"latest--{session}__{agent}{ext}")
        for lnk in links:
            lnk.unlink(missing_ok=True)
            try:
                lnk.symlink_to(archive.name)
            except OSError:
                pass
        # Return the most-specific symlink rather than the archive file. mpv
        # reports back whatever path was passed to `loadfile`, and tts-ctl's
        # `toggle` compares mpv's `path` property to the target resolved by
        # `replay_target` (which always returns a `latest--…` symlink). If
        # play() loadfiled the timestamped archive, every Space hit would
        # see a path mismatch and trigger a reload-from-start. Loading the
        # symlink keeps those equal and lets toggle cycle pause cheaply.
        if session and host_in_stem:
            most_specific = state / f"latest--{host_in_stem}--{session}{ext}"
        elif session:
            most_specific = state / f"latest--{session}{ext}"
        else:
            most_specific = state / f"latest{ext}"
        return most_specific if most_specific.exists() else archive

    def play(self, path: Path) -> bool:
        playable = self._update_latest(path)
        # IPC mode: load the archived copy into running mpv. The queue entry is
        # deleted after play() returns, and mpv may not open append-play files
        # until after the IPC command has returned.
        if self.ipc_socket:
            # Defensive unpause: a paused mpv with append-play queues new
            # files behind the paused one, so the entire watcher queue
            # silently stalls until someone manually resumes playback.
            self._send_ipc(["set_property", "pause", False])
            resp = self._send_ipc(["loadfile", str(playable), "append-play"])
            if resp and resp.get("error") == "success":
                return True
            return False

        # Direct invocation
        cmd = [self.bin, "--no-video", "--really-quiet"] + self.extra_args + [str(playable)]
        try:
            if self.wait:
                subprocess.run(cmd, check=True, timeout=300)
            else:
                self._proc = subprocess.Popen(cmd)
            return True
        except (subprocess.SubprocessError, OSError):
            return False

    def describe(self) -> str:
        suffix = f" → {self.target}" if self.target else ""
        if self.ipc_socket:
            return f"mpv (IPC: {self.ipc_socket}{suffix})"
        return f"mpv ({self.bin}{suffix})"
