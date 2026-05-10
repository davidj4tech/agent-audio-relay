"""aar-termux-setup — idempotent Termux/runit installer + updater for AAR.

This is intentionally separate from ``aar-setup`` because Termux commonly uses
runit via ``termux-services`` instead of systemd user units.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_REPO = "https://github.com/davidj4tech/agent-audio-relay.git"


AGENT_AUDIO_RELAY_RUN = r'''#!{prefix}/bin/sh
exec 2>&1
STATE="${XDG_STATE_HOME:-$HOME/.local/state}/agent-audio-relay"
SOCK="$STATE/mpv-tts.sock"
i=0
while [ $i -lt 30 ]; do
  [ -S "$SOCK" ] && break
  i=$((i+1)); sleep 1
done

mkdir -p "$STATE" "$HOME/.cache/agent-audio-relay/tts-claude" "$HOME/.cache/agent-audio-relay/tts-pi" "$HOME/.cache/agent-audio-relay/queue"

export RELAY_BACKEND="${RELAY_BACKEND:-mpv}"
export RELAY_MPV_SOCKET="${RELAY_MPV_SOCKET:-$SOCK}"
export RELAY_MPV_WAIT="${RELAY_MPV_WAIT:-1}"
export RELAY_WATCH_DIRS="${RELAY_WATCH_DIRS:-$HOME/.cache/agent-audio-relay}"
export RELAY_QUEUE_DIR="${RELAY_QUEUE_DIR:-$HOME/.cache/agent-audio-relay/queue}"
export RELAY_STATE_FILE="${RELAY_STATE_FILE:-$HOME/.cache/agent-audio-relay/delivered.txt}"
export RELAY_PAD_SILENCE="${RELAY_PAD_SILENCE:-0}"

exec "$HOME/.local/bin/agent-audio-relay"
'''


MPV_TTS_RUN = r'''#!{prefix}/bin/sh
exec 2>&1
STATE="${XDG_STATE_HOME:-$HOME/.local/state}/agent-audio-relay"
SOCK="$STATE/mpv-tts.sock"
LEGACY="$PREFIX/tmp/mpv-tts.sock"
mkdir -p "$STATE"
rm -f "$SOCK" "$LEGACY"
ln -s "$SOCK" "$LEGACY"
# --ao=opensles so Android audio focus pauses TTS on incoming calls.
exec mpv --idle=yes --ao=opensles --force-window=no --input-ipc-server="$SOCK" --volume=100 --keep-open=no
'''


MPV_VOICE_RUN = r'''#!{prefix}/bin/sh
exec 2>&1
STATE="${XDG_STATE_HOME:-$HOME/.local/state}/agent-audio-relay"
SOCK="$STATE/mpv-voice.sock"
LEGACY="$PREFIX/tmp/mpv-voice.sock"
mkdir -p "$STATE"
rm -f "$SOCK" "$LEGACY"
ln -s "$SOCK" "$LEGACY"
exec mpv --idle=yes --force-window=no --input-ipc-server="$SOCK" --ytdl=yes --volume=85
'''


MPV_MUSIC_RUN = r'''#!{prefix}/bin/sh
exec 2>&1
STATE="${XDG_STATE_HOME:-$HOME/.local/state}/agent-audio-relay"
SOCK="$STATE/mpv-music.sock"
LEGACY="$PREFIX/tmp/mpv-music.sock"
mkdir -p "$STATE"
rm -f "$SOCK" "$LEGACY"
ln -s "$SOCK" "$LEGACY"
exec mpv --idle=yes --force-window=no --input-ipc-server="$SOCK" --ytdl=yes --volume=70
'''


SERVICE_RUNS = {
    "agent-audio-relay": AGENT_AUDIO_RELAY_RUN,
    "mpv-tts": MPV_TTS_RUN,
    "mpv-voice": MPV_VOICE_RUN,
    "mpv-music": MPV_MUSIC_RUN,
}


def prefix() -> Path:
    return Path(os.environ.get("PREFIX", "/data/data/com.termux/files/usr"))


def service_root() -> Path:
    return prefix() / "var" / "service"


def is_termux() -> bool:
    p = str(prefix())
    return "com.termux" in p or shutil.which("termux-info") is not None


def run(cmd: list[str], *, check: bool = True, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(str(c) for c in cmd), flush=True)
    return subprocess.run(cmd, cwd=cwd, check=check, text=True)


def have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def selected_services(args) -> list[str]:
    return args.services or list(SERVICE_RUNS)


def install_service(name: str, *, force: bool = False) -> bool:
    if name not in SERVICE_RUNS:
        print(f"unknown service: {name}", file=sys.stderr)
        return False
    root = service_root()
    svc = root / name
    log = svc / "log"
    run_path = svc / "run"
    content = SERVICE_RUNS[name].replace("{prefix}", str(prefix())).encode()

    svc.mkdir(parents=True, exist_ok=True)
    log.mkdir(parents=True, exist_ok=True)

    changed = True
    if run_path.exists() and run_path.read_bytes() == content:
        changed = False
    elif run_path.exists() and not force:
        backup = run_path.with_name(f"run.bak-{int(run_path.stat().st_mtime)}")
        shutil.copy2(run_path, backup)
        print(f"  backed up custom {run_path} -> {backup}")

    if changed:
        run_path.write_bytes(content)
        run_path.chmod(0o755)
        print(f"  wrote {run_path}")
    else:
        print(f"  ok {run_path}")
    return changed


def restart_service(name: str) -> None:
    if have("sv"):
        run(["sv", "restart", name], check=False)
    else:
        print(f"sv not found; restart manually: {name}")


def cmd_install(args) -> int:
    if not is_termux() and not args.yes:
        print("This does not look like Termux. Re-run with --yes to install anyway.", file=sys.stderr)
        return 2
    for dep in ("mpv", "inotifywait"):
        if not have(dep):
            print(f"warning: {dep} not found on PATH")
    changed_any = False
    for name in selected_services(args):
        changed_any = install_service(name, force=args.force) or changed_any
    if args.start:
        for name in selected_services(args):
            restart_service(name)
    elif changed_any:
        print("\nTo start/restart:")
        for name in selected_services(args):
            print(f"  sv restart {name}")
    return 0


def ensure_repo(path: Path, url: str) -> None:
    if (path / ".git").is_dir():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    run(["git", "clone", url, str(path)])


def cmd_update(args) -> int:
    repo = Path(args.repo).expanduser()
    ensure_repo(repo, args.url)
    if not args.no_pull:
        run(["git", "pull", "--ff-only"], cwd=repo)
    run([sys.executable, "-m", "pip", "install", "--user", "--upgrade", str(repo)])
    if args.install_services:
        class InstallArgs:
            services = args.services
            force = args.force
            start = False
            yes = True
        cmd_install(InstallArgs())
    if args.restart:
        for name in selected_services(args):
            restart_service(name)
    return 0


def cmd_status(args) -> int:
    print(f"Termux: {'yes' if is_termux() else 'no'}")
    print(f"PREFIX: {prefix()}")
    print(f"services: {service_root()}")
    if have("sv"):
        for name in selected_services(args):
            run(["sv", "status", name], check=False)
    else:
        for name in selected_services(args):
            print(f"{name}: {'installed' if (service_root()/name/'run').exists() else 'missing'}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        prog="aar-termux-setup",
        description="Idempotent Termux/runit installer and updater for agent-audio-relay.",
    )
    p.add_argument("--yes", action="store_true", help="assume yes / allow non-Termux")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("install", help="install/update runit service run files")
    pi.add_argument("services", nargs="*", choices=sorted(SERVICE_RUNS), help="services to install (default: all)")
    pi.add_argument("--force", action="store_true", help="overwrite existing run files without backup")
    pi.add_argument("--start", action="store_true", help="restart services after install")
    pi.set_defaults(func=cmd_install)

    pu = sub.add_parser("update", help="git pull, pip install --upgrade, then restart services")
    pu.add_argument("--repo", default="~/projects/agent-audio-relay", help="local checkout path")
    pu.add_argument("--url", default=DEFAULT_REPO, help="repo URL to clone if --repo is missing")
    pu.add_argument("--no-pull", action="store_true", help="skip git pull")
    pu.add_argument("--no-restart", dest="restart", action="store_false", help="do not restart services")
    pu.add_argument("--install-services", action="store_true", help="also refresh runit service files")
    pu.add_argument("--force", action="store_true", help="with --install-services, overwrite run files without backup")
    pu.add_argument("services", nargs="*", choices=sorted(SERVICE_RUNS), help="services to restart/install (default: all)")
    pu.set_defaults(func=cmd_update, restart=True)

    ps = sub.add_parser("status", help="show Termux/runit service status")
    ps.add_argument("services", nargs="*", choices=sorted(SERVICE_RUNS), help="services to show (default: all)")
    ps.set_defaults(func=cmd_status)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
