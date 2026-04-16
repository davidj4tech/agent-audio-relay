"""SSH + termux-media-player playback backend.

Delivers audio to a remote device (typically an Android phone running Termux)
via SCP, then plays it with termux-media-player over SSH.

Environment variables:
    RELAY_SSH_HOST          SSH alias for the target device (default: p8ar)
    RELAY_SSH_DEST          Remote path prefix for audio files (default: .cache/relay-latest)
    RELAY_SSH_MAX_RETRIES   Retry count for SCP/play (default: 2)
    RELAY_SSH_PLAYBACK_WAIT Max seconds to wait for current playback (default: 120)
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

from .base import PlaybackBackend


class SshTermuxBackend(PlaybackBackend):
    name = "ssh-termux"

    def __init__(self) -> None:
        self.host = os.environ.get("RELAY_SSH_HOST", "p8ar")
        self.dest = os.environ.get("RELAY_SSH_DEST", ".cache/relay-latest")
        self.max_retries = int(os.environ.get("RELAY_SSH_MAX_RETRIES", "2"))
        self.max_wait = int(os.environ.get("RELAY_SSH_PLAYBACK_WAIT", "120"))

    def _ssh(self, cmd: str, timeout: int = 10) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
             self.host, cmd],
            capture_output=True, text=True, timeout=timeout,
        )

    @staticmethod
    def _mmss_to_s(mmss: str) -> int:
        parts = mmss.split(":")
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        return 0

    def wait_for_playback(self) -> None:
        waited = 0
        while waited < self.max_wait:
            try:
                info = self._ssh("termux-media-player info").stdout
            except (subprocess.SubprocessError, OSError):
                break

            if "playing" not in info.lower():
                break

            times = re.findall(r"\d+:\d+", info)
            if len(times) >= 2:
                remaining = self._mmss_to_s(times[1]) - self._mmss_to_s(times[0])
                if remaining <= 0:
                    break
                wait_time = min(remaining + 1, self.max_wait - waited)
                time.sleep(wait_time)
                waited += wait_time
            else:
                time.sleep(1)
                waited += 1

    def play(self, path: Path) -> bool:
        ext = path.suffix.lstrip(".")
        dest = f"{self.dest}.{ext}"

        for attempt in range(1, self.max_retries + 1):
            try:
                self._ssh("mkdir -p .cache")
                subprocess.run(
                    ["scp", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                     str(path), f"{self.host}:{dest}"],
                    check=True, capture_output=True, timeout=30,
                )
                self._ssh(f"termux-media-player play '{dest}'")
                return True
            except (subprocess.SubprocessError, OSError):
                if attempt < self.max_retries:
                    time.sleep(2)
        return False

    def describe(self) -> str:
        return f"ssh-termux ({self.host})"
