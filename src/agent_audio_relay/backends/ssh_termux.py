"""SSH + remote player playback backend.

Delivers audio to a remote device (typically an Android phone running Termux)
via SCP, then plays it remotely. Two players are supported:

  * `termux-media-player` (default) — fire-and-forget; no seek/volume.
  * `mpv-ipc` — sends `loadfile` to a long-running mpv daemon's
    JSON-IPC socket via `socat`. Required if you want `tts-ctl`/the
    floating popup's seek/volume/status controls to talk to the same
    audio you're hearing.

Environment variables:
    RELAY_SSH_HOST              SSH alias for the target device (default: p8ar)
    RELAY_SSH_DEST              Remote path prefix for audio files (default: .cache/relay-latest)
    RELAY_SSH_MAX_RETRIES       Retry count for SCP/play (default: 2)
    RELAY_SSH_PLAYBACK_WAIT     Max seconds to wait for current playback (default: 120)
    RELAY_TERMUX_PLAYER         `termux-media-player` or `mpv-ipc`. Unset =
                                auto-detect: probe the mpv IPC socket and use
                                mpv-ipc if reachable, else termux-media-player.
    RELAY_TERMUX_MPV_SOCK       Remote path of the mpv IPC socket
                                (default: $PREFIX/tmp/mpv-tts.sock,
                                resolved on the phone as
                                /data/data/com.termux/files/usr/tmp/mpv-tts.sock).
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
import socket
import subprocess
import time
from pathlib import Path


def _host_slug() -> str:
    """Slug the local hostname the same way `tts-ctl` will on replay.

    Two hosts emitting clips for tmux sessions with the same name (e.g. both
    have `main`) would otherwise overwrite each other's `latest--<session>`
    symlink on the phone. Adding a `latest--<host>--<session>` symlink and
    preferring it on replay disambiguates them.
    """
    raw = socket.gethostname().split(".", 1)[0].lower()
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9-]+", "-", raw)).strip("-")

from .base import PlaybackBackend, original_name


class SshTermuxBackend(PlaybackBackend):
    name = "ssh-termux"

    def __init__(self, target: str | None = None) -> None:
        self.host = os.environ.get("RELAY_SSH_HOST", "p8ar")
        self.dest = os.environ.get("RELAY_SSH_DEST", ".cache/relay-latest")
        self.max_retries = int(os.environ.get("RELAY_SSH_MAX_RETRIES", "2"))
        self.max_wait = int(os.environ.get("RELAY_SSH_PLAYBACK_WAIT", "120"))
        self.switch_cmd = os.environ.get("RELAY_TERMUX_SWITCH_CMD", "").strip()
        self.mpv_sock = os.environ.get(
            "RELAY_TERMUX_MPV_SOCK",
            "/data/data/com.termux/files/usr/tmp/mpv-tts.sock",
        )
        # mpv-ipc is strictly better than termux-media-player (queueing, seek,
        # volume, accurate result reporting) but needs a long-running mpv on
        # the remote with --input-ipc-server. Auto-detect when the user
        # didn't pin a choice: probe the socket once and pick mpv-ipc if
        # something's listening.
        explicit = os.environ.get("RELAY_TERMUX_PLAYER", "").strip()
        if explicit:
            self.player = explicit
        else:
            self.player = self._detect_player()
        self.target = target
        self._last_switched: str | None = None

    def _detect_player(self) -> str:
        probe = (
            f"test -S {shlex.quote(self.mpv_sock)} && "
            f"socat -u /dev/null UNIX-CONNECT:{shlex.quote(self.mpv_sock)}"
        )
        try:
            rc = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3",
                 self.host, probe],
                capture_output=True, text=True, timeout=8,
            ).returncode
        except (subprocess.SubprocessError, OSError):
            return "termux-media-player"
        return "mpv-ipc" if rc == 0 else "termux-media-player"

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

    def _mpv_remote(self, json_payload: str) -> str:
        """Run a one-shot mpv IPC command on the remote and return stdout."""
        remote = (
            f"printf '%s\\n' {shlex.quote(json_payload)}"
            f" | socat - UNIX-CONNECT:{shlex.quote(self.mpv_sock)}"
        )
        try:
            return self._ssh(remote).stdout
        except (subprocess.SubprocessError, OSError):
            return ""

    def wait_for_playback(self) -> None:
        waited = 0
        while waited < self.max_wait:
            if self.player == "mpv-ipc":
                resp = self._mpv_remote('{"command":["get_property","idle-active"]}')
                if '"data":true' in resp or not resp:
                    break
                time.sleep(1)
                waited += 1
                continue

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
                    host = _host_slug()
                    if host:
                        # Host-prefixed pointer: tts-ctl's replay_target prefers
                        # this so cross-host session-name collisions don't pick
                        # up a clip from another machine.
                        links.append(f".cache/agent-audio/latest--{host}--{session}.{ext}")
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
                self._ssh(ln_cmds)
                if self.player == "mpv-ipc":
                    # Resolve $HOME on the phone — mpv loadfile needs an
                    # absolute path. .cache/... is relative to ~ on Termux.
                    abs_path = (
                        "/data/data/com.termux/files/home/" + archive
                    )
                    payload = (
                        '{"command":["loadfile",'
                        f'"{abs_path}",'
                        '"replace"]}'
                    )
                    self._mpv_remote(payload)
                    # New file should start playing — make sure pause is off.
                    self._mpv_remote('{"command":["set_property","pause",false]}')
                else:
                    self._ssh(f"termux-media-player play '{archive}'")
                return True
            except (subprocess.SubprocessError, OSError):
                if attempt < self.max_retries:
                    time.sleep(2)
        return False

    def describe(self) -> str:
        player = self.player if self.player != "termux-media-player" else "tmp"
        if self.target:
            return f"ssh-termux/{player} ({self.host} → {self.target})"
        return f"ssh-termux/{player} ({self.host})"
