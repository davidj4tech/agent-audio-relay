"""Thin launchers that exec the packaged shell scripts.

Each function is wired up as a `[project.scripts]` entry in pyproject.toml,
so `pip install` puts e.g. `tts-drop` and `claude-code-tts-hook` into
`~/.local/bin/` (or the pipx venv) as console scripts. The launcher resolves
the bash script's path inside the installed package and `os.execv`s into it,
preserving argv, stdin, stdout, stderr, and the script's own `$0` so its
relative-path sources (`$(dirname "$0")/../hooks/lib/...`) keep working.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _shell_root() -> Path:
    return Path(__file__).resolve().parent / "shell"


def _exec(rel: str) -> None:
    script = _shell_root() / rel
    if not script.exists():
        sys.stderr.write(f"agent-audio-relay: missing packaged script: {script}\n")
        sys.exit(127)
    # bash explicitly so the executable bit isn't load-bearing across wheels.
    os.execv("/bin/bash", ["/bin/bash", str(script), *sys.argv[1:]])


def tts_drop() -> None:           _exec("bin/tts-drop")
def tts_ctl() -> None:            _exec("bin/tts-ctl")
def tts_popup() -> None:          _exec("bin/tts-popup")
def tts_pick() -> None:           _exec("bin/tts-pick")
def tts_status_line() -> None:    _exec("bin/tts-status-line")
def mpv_tunnel() -> None:         _exec("bin/aar-mpv-tunnel")
def forwarder() -> None:          _exec("bin/agent-audio-relay-forwarder.sh")
def claude_code_hook() -> None:   _exec("hooks/claude-code-tts-hook.sh")
def opencode_hook() -> None:      _exec("hooks/opencode-tts-hook.sh")
def codex_hook() -> None:         _exec("hooks/codex-tts-hook.sh")
def ha_bridge() -> None:          _exec("hooks/ha-tts-bridge.sh")


def tts_tmux_install() -> None:
    """Exec the packaged tts.tmux to register the popup/status bindings.

    Designed to be sourced from a tmux config via `run-shell`, e.g.

        run-shell "tts-tmux-install"

    This is the non-TPM install path. It works for users whose config
    loader (gpakosz/oh-my-tmux's `run`-based .tmux.conf.local; any
    custom split-file setup) doesn't expose `set -g @plugin` lines to
    TPM's plugin-discovery scanner. The TPM path
    (`set -g @plugin 'davidj4tech/agent-audio-relay'`) still works for
    users whose config TPM can scan.

    The script lives at `agent_audio_relay/tts.tmux` inside the wheel
    via `[tool.hatch.build.targets.wheel.force-include]` (pyproject.toml).
    """
    script = Path(__file__).resolve().parent / "tts.tmux"
    if not script.exists():
        sys.stderr.write(
            f"agent-audio-relay: missing packaged tts.tmux: {script}\n"
            "Try `pip install --force-reinstall agent-audio-relay`.\n"
        )
        sys.exit(127)
    os.execv("/bin/bash", ["/bin/bash", str(script), *sys.argv[1:]])


def hooks_dir() -> None:
    """Print the install path of the hooks directory.

    Lets systemd units / .claude/settings.json reference hooks reproducibly:
        ExecStart=/bin/bash -c 'exec "$(agent-audio-relay-hooks-dir)/opencode-tts-hook.sh"'
    """
    print(_shell_root() / "hooks")
