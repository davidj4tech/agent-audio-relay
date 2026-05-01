"""SSH + remote mpv playback backend.

Delivers audio to a Linux box (typically homer) via SCP, then plays it with
mpv over SSH. Unlike ssh-termux, the remote host is PipeWire/PulseAudio-based,
so `target` maps to a sink name (e.g. `bluez_output.CC_4C_8B_21_98_99.1`) and
is passed to mpv as `--audio-device=pulse/<target>`.

This enables routing different audio streams to different Bluetooth sinks on
the same remote host — the groundwork for concurrent multi-lane playback.

Environment variables:
    RELAY_SSH_MPV_HOST          SSH alias for the target host (default: homer)
    RELAY_SSH_MPV_DEST          Remote path prefix for audio files
                                (default: .cache/relay-latest)
    RELAY_SSH_MPV_BIN           Remote mpv binary (default: mpv)
    RELAY_SSH_MPV_ARGS          Extra mpv arguments, space-separated (default: "")
    RELAY_SSH_MPV_WAIT          Block on playback: 1 or 0 (default: 1). Set to 0
                                for fire-and-forget (useful once multi-lane
                                routing lands — lets independent lanes overlap).
    RELAY_SSH_MPV_MAX_RETRIES   Retry count for SCP/play (default: 2)
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

from .base import PlaybackBackend


class SshMpvBackend(PlaybackBackend):
    name = "ssh-mpv"

    def __init__(self, target: str | None = None) -> None:
        self.host = os.environ.get("RELAY_SSH_MPV_HOST", "homer")
        self.dest = os.environ.get("RELAY_SSH_MPV_DEST", ".cache/relay-latest")
        self.bin = os.environ.get("RELAY_SSH_MPV_BIN", "mpv")
        self.extra_args = (
            os.environ.get("RELAY_SSH_MPV_ARGS", "").split()
            if os.environ.get("RELAY_SSH_MPV_ARGS")
            else []
        )
        self.wait = os.environ.get("RELAY_SSH_MPV_WAIT", "1") == "1"
        self.max_retries = int(os.environ.get("RELAY_SSH_MPV_MAX_RETRIES", "2"))
        self.target = target
        self._proc: subprocess.Popen | None = None

    @staticmethod
    def _log(msg: str) -> None:
        print(f"[ssh-mpv] {msg}", file=sys.stderr, flush=True)

    def _audio_device_arg(self) -> str | None:
        if not self.target:
            return None
        device = self.target if "/" in self.target else f"pulse/{self.target}"
        return f"--audio-device={device}"

    def _remote_mpv_cmd(self, remote_path: str) -> str:
        parts = [shlex.quote(self.bin), "--no-video", "--really-quiet"]
        dev = self._audio_device_arg()
        if dev:
            parts.append(shlex.quote(dev))
        for a in self.extra_args:
            parts.append(shlex.quote(a))
        parts.append(shlex.quote(remote_path))
        return " ".join(parts)

    def wait_for_playback(self) -> None:
        # Blocking mode waits inside play(); nothing to do here. In
        # fire-and-forget mode we intentionally don't block — concurrent
        # lanes will overlap, which is the point.
        if self._proc is not None and self.wait:
            try:
                self._proc.wait(timeout=300)
            except subprocess.TimeoutExpired:
                pass
            self._proc = None

    def play(self, path: Path) -> bool:
        ext = path.suffix.lstrip(".")
        dest = f"{self.dest}.{ext}"
        remote_cmd = self._remote_mpv_cmd(dest)

        for attempt in range(1, self.max_retries + 1):
            try:
                subprocess.run(
                    ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                     self.host, "mkdir -p .cache"],
                    check=True, capture_output=True, timeout=10,
                )
                subprocess.run(
                    ["scp", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                     str(path), f"{self.host}:{dest}"],
                    check=True, capture_output=True, timeout=30,
                )
                ssh_cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                           self.host, remote_cmd]
                if self.wait:
                    subprocess.run(ssh_cmd, check=True, timeout=300)
                else:
                    self._proc = subprocess.Popen(
                        ssh_cmd,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                return True
            except (subprocess.SubprocessError, OSError) as e:
                if attempt < self.max_retries:
                    time.sleep(2)
                else:
                    self._log(f"PLAY:FAIL {path.name} ({e.__class__.__name__})")
        return False

    def describe(self) -> str:
        if self.target:
            return f"ssh-mpv ({self.host} → {self.target})"
        return f"ssh-mpv ({self.host})"
