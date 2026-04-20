"""SSH + termux-media-player playback backend.

Delivers audio to a remote device (typically an Android phone running Termux)
via SCP, then plays it with termux-media-player over SSH.

Environment variables:
    RELAY_SSH_HOST              SSH alias for the target device (default: p8ar)
    RELAY_SSH_DEST              Remote path prefix for audio files (default: .cache/relay-latest)
    RELAY_SSH_MAX_RETRIES       Retry count for SCP/play (default: 2)
    RELAY_SSH_PLAYBACK_WAIT     Max seconds to wait for current playback (default: 120)
    RELAY_TERMUX_SWITCH_CMD     Remote command template invoked before playing when
                                the selected target changes. The target is appended
                                as a shell-quoted argument. Unset = no-op (audio
                                routes to whichever BT device Android considers
                                active). Example:
                                    su -c "cmd bluetooth_manager connect"
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import time
from pathlib import Path

from .base import PlaybackBackend, original_name


class SshTermuxBackend(PlaybackBackend):
    name = "ssh-termux"

    def __init__(self, target: str | None = None) -> None:
        self.host = os.environ.get("RELAY_SSH_HOST", "p8ar")
        self.dest = os.environ.get("RELAY_SSH_DEST", ".cache/relay-latest")
        self.max_retries = int(os.environ.get("RELAY_SSH_MAX_RETRIES", "2"))
        self.max_wait = int(os.environ.get("RELAY_SSH_PLAYBACK_WAIT", "120"))
        self.switch_cmd = os.environ.get("RELAY_TERMUX_SWITCH_CMD", "").strip()
        self.target = target
        self._last_switched: str | None = None

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

    def _maybe_switch_bt(self) -> None:
        """Run the user-supplied BT-switch command on the remote if target changed."""
        if self.target is None or self.target == self._last_switched:
            return
        if not self.switch_cmd:
            # Target was requested but no switch command configured — record
            # anyway so we don't log BT:SKIPPED on every audio file.
            self._log("BT:SKIPPED (no RELAY_TERMUX_SWITCH_CMD configured)")
            self._last_switched = self.target
            return

        remote_cmd = f"{self.switch_cmd} {shlex.quote(self.target)}"
        try:
            result = self._ssh(remote_cmd, timeout=15)
            if result.returncode == 0:
                self._log(f"BT:SWITCH {self.target}")
                self._last_switched = self.target
            else:
                err = (result.stderr or result.stdout or "").strip().splitlines()[-1:]
                self._log(f"BT:FAIL {self.target} rc={result.returncode} {' '.join(err)}")
        except (subprocess.SubprocessError, OSError) as e:
            self._log(f"BT:FAIL {self.target} ({e.__class__.__name__})")

    @staticmethod
    def _log(msg: str) -> None:
        import sys
        print(f"[ssh-termux] {msg}", file=sys.stderr, flush=True)

    def play(self, path: Path) -> bool:
        self._maybe_switch_bt()
        ext = path.suffix.lstrip(".")
        name = original_name(path)
        archive = f".cache/agent-audio/{name}"

        # Build latest pointers: global + per-session (+ per-session__agent).
        # Stem: YYYYMMDDTHHMMSS--<session>__<persona>_<agent>_<kind>
        links = [f".cache/agent-audio/latest.{ext}"]
        stem = Path(name).stem
        if "--" in stem and "__" in stem:
            try:
                after_ts = stem.split("--", 1)[1]
                session, rest = after_ts.split("__", 1)
                parts = rest.split("_")
                if session:
                    links.append(f".cache/agent-audio/latest--{session}.{ext}")
                    if len(parts) >= 3:
                        agent = parts[-2]
                        if agent:
                            links.append(f".cache/agent-audio/latest--{session}__{agent}.{ext}")
            except ValueError:
                pass
        ln_cmds = " && ".join(f"ln -sf '{name}' '{lnk}'" for lnk in links)

        for attempt in range(1, self.max_retries + 1):
            try:
                self._ssh("mkdir -p .cache/agent-audio")
                subprocess.run(
                    ["scp", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                     str(path), f"{self.host}:{archive}"],
                    check=True, capture_output=True, timeout=30,
                )
                self._ssh(f"{ln_cmds} && termux-media-player play '{archive}'")
                return True
            except (subprocess.SubprocessError, OSError):
                if attempt < self.max_retries:
                    time.sleep(2)
        return False

    def describe(self) -> str:
        if self.target:
            return f"ssh-termux ({self.host} → {self.target})"
        return f"ssh-termux ({self.host})"
