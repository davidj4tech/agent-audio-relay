"""aar-setup — install/uninstall AAR systemd user services and check prereqs.

Subcommands:
    aar-setup check [units...]       — verify prereq binaries are on PATH
    aar-setup install [units...]     — copy unit files into ~/.config/systemd/user/
    aar-setup install --start [...]  — also enable+start them
    aar-setup uninstall [units...]   — stop+disable+remove
    aar-setup status                 — show install + active state
    aar-setup list                   — list units this package ships

Why a Python tool instead of a shell script: needs to introspect the
installed package's systemd/ dir (works whether installed via pip or
running from a source checkout), and prefer per-distro-aware prereq
hints — neither of which is fun in bash.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Per-component prereqs. Each entry: list of (binary, distro→pkg-name).
# Keep keys aligned with unit-file basenames (without .service).
PREREQS: dict[str, list[tuple[str, dict[str, str]]]] = {
    "aar-mpv-tunnel": [
        ("ssh",   {"fedora": "openssh-clients", "debian": "openssh-client"}),
        ("socat", {"fedora": "socat",           "debian": "socat"}),
        ("jq",    {"fedora": "jq",              "debian": "jq"}),
    ],
    "aar-sink-stream": [
        ("pactl",  {"fedora": "pulseaudio-utils", "debian": "pulseaudio-utils"}),
        ("ffmpeg", {"fedora": "ffmpeg",           "debian": "ffmpeg"}),
    ],
    "aar-clip-server": [],
    "agent-audio-relay-forwarder": [
        ("rsync", {"fedora": "rsync",            "debian": "rsync"}),
        ("ssh",   {"fedora": "openssh-clients",  "debian": "openssh-client"}),
    ],
    # agent-audio-relay.service runs the watcher (Python module). No extra
    # binary prereqs beyond the package itself.
    "agent-audio-relay": [],
    "opencode-tts-watcher": [],
}


def units_dir() -> Path:
    """Locate the AAR systemd/ directory.

    Two layouts are supported:
      1. Installed wheel — `systemd/` is force-included into
         `agent_audio_relay/systemd/`. `Path(__file__).parent / "systemd"`
         finds it.
      2. Source checkout — `systemd/` lives at the repo root. We walk
         up looking for a `systemd/` containing a known unit file.
    """
    pkg = Path(__file__).resolve().parent / "systemd"
    if pkg.is_dir() and (pkg / "aar-mpv-tunnel.service").is_file():
        return pkg
    # Walk up looking for the source-tree systemd/.
    for ancestor in Path(__file__).resolve().parents:
        cand = ancestor / "systemd"
        if cand.is_dir() and (cand / "aar-mpv-tunnel.service").is_file():
            return cand
    raise RuntimeError(
        "could not locate AAR systemd/ directory "
        "(neither in package nor any parent)"
    )


def detect_distro() -> str:
    """Return 'fedora', 'debian', or 'unknown' based on /etc/os-release."""
    try:
        text = Path("/etc/os-release").read_text()
    except OSError:
        return "unknown"
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            fields[k] = v.strip().strip('"')
    id_ = fields.get("ID", "").lower()
    like = fields.get("ID_LIKE", "").lower()
    if id_ in {"fedora", "rhel", "centos", "rocky", "almalinux"} or "rhel" in like or "fedora" in like:
        return "fedora"
    if id_ in {"debian", "ubuntu"} or "debian" in like:
        return "debian"
    return "unknown"


def user_units_dir() -> Path:
    cfg = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return cfg / "systemd" / "user"


def list_units() -> list[str]:
    return sorted(p.name for p in units_dir().glob("*.service"))


def _normalize_unit(name: str) -> str:
    return name if name.endswith(".service") else f"{name}.service"


def cmd_list(_args) -> int:
    print(f"AAR units shipped with this install (source: {units_dir()}):")
    for name in list_units():
        print(f"  {name}")
    return 0


def cmd_check(args) -> int:
    distro = detect_distro()
    targets = [t.replace(".service", "") for t in args.units] or list(PREREQS.keys())
    missing: dict[str, list[tuple[str, str]]] = {}
    for unit in targets:
        for cmd, pkgs in PREREQS.get(unit, []):
            if shutil.which(cmd) is None:
                missing.setdefault(unit, []).append((cmd, pkgs.get(distro, "?")))
    if not missing:
        print("aar-setup check: all required commands present.")
        return 0
    print("aar-setup check: missing commands:")
    pkgs_to_install: set[str] = set()
    for unit, items in missing.items():
        print(f"  {unit}:")
        for cmd, pkg in items:
            print(f"    - {cmd} (package: {pkg})")
            if pkg and pkg != "?":
                pkgs_to_install.add(pkg)
    if pkgs_to_install:
        print()
        if distro == "fedora":
            print(f"  Install: sudo dnf install {' '.join(sorted(pkgs_to_install))}")
        elif distro == "debian":
            print(f"  Install: sudo apt install {' '.join(sorted(pkgs_to_install))}")
        else:
            print(f"  Install via your package manager: {' '.join(sorted(pkgs_to_install))}")
    return 1


def cmd_install(args) -> int:
    src = units_dir()
    dst = user_units_dir()
    dst.mkdir(parents=True, exist_ok=True)
    targets = [_normalize_unit(t) for t in args.units] or list_units()
    installed: list[str] = []
    for name in targets:
        src_path = src / name
        if not src_path.is_file():
            print(f"aar-setup install: unit not found: {name}", file=sys.stderr)
            return 2
        dst_path = dst / name
        shutil.copy2(src_path, dst_path)
        installed.append(name)
        print(f"  + {dst_path}")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    if args.start:
        for name in installed:
            print(f"  enable+start {name}")
            subprocess.run(
                ["systemctl", "--user", "enable", "--now", name], check=False
            )
    else:
        print()
        print("To start them:")
        for name in installed:
            print(f"  systemctl --user enable --now {name}")
    return 0


def cmd_uninstall(args) -> int:
    dst = user_units_dir()
    targets = [_normalize_unit(t) for t in args.units] or list_units()
    removed: list[str] = []
    for name in targets:
        path = dst / name
        if not path.exists():
            continue
        subprocess.run(
            ["systemctl", "--user", "disable", "--now", name],
            check=False, capture_output=True,
        )
        path.unlink()
        removed.append(name)
        print(f"  - {path}")
    if removed:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        return 0
    print("aar-setup uninstall: nothing to remove.")
    return 0


def cmd_status(_args) -> int:
    dst = user_units_dir()
    print(f"AAR units (source: {units_dir()}):")
    print(f"{'unit':<40} {'installed':<14} {'state'}")
    for name in list_units():
        link = dst / name
        is_installed = link.exists()
        if is_installed:
            r = subprocess.run(
                ["systemctl", "--user", "is-active", name],
                capture_output=True, text=True,
            )
            state = r.stdout.strip() or "inactive"
        else:
            state = "-"
        print(f"  {name:<38} {('yes' if is_installed else 'no'):<14} {state}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        prog="aar-setup",
        description="Install/uninstall AAR systemd user services and check prereqs.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pc = sub.add_parser("check", help="check that prereq binaries are on PATH")
    pc.add_argument("units", nargs="*", help="restrict to these units (default: all)")
    pc.set_defaults(func=cmd_check)

    pi = sub.add_parser("install", help="copy unit files into ~/.config/systemd/user/")
    pi.add_argument("units", nargs="*", help="install only these (default: all)")
    pi.add_argument("--start", action="store_true", help="enable+start after install")
    pi.set_defaults(func=cmd_install)

    pu = sub.add_parser("uninstall", help="stop+disable+remove unit files")
    pu.add_argument("units", nargs="*", help="remove only these (default: all)")
    pu.set_defaults(func=cmd_uninstall)

    ps = sub.add_parser("status", help="show install + active state for each unit")
    ps.set_defaults(func=cmd_status)

    pl = sub.add_parser("list", help="list units this package ships")
    pl.set_defaults(func=cmd_list)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
