"""aar-snap — CLI wrapper for Snapcast's JSON-RPC.

Snapcast's web UI handles routing perfectly well, but for scripting and
parity with `aar-where` we want a terse CLI. This is roughly the same
shape: tell room X to listen to channel Y, set its volume, mute it.

Multiple snapservers (mel, sp4r) are addressed transparently — config
maps a logical channel name to (server, stream-id), and a logical room
name to (server, client-id-or-name).

Config: ~/.config/agent-audio-relay/aar-snap.json — see header of
`config.py` for schema, or run `aar-snap list` to see the current state
matching whatever you've configured.

Subcommands:
    aar-snap list                       — full state across configured servers
    aar-snap rooms                      — short room/stream list
    aar-snap streams                    — all streams across all servers
    aar-snap join <channel> <room>      — make `room` listen to `channel`
    aar-snap volume <room> <0-100>      — set room volume
    aar-snap mute <room> [on|off]       — toggle mute (no arg = toggle)

`channel` is a key from config.streams; `room` is a key from
config.rooms (or a snapcast client id / name if no alias is configured).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

CONFIG_PATH = Path(
    os.environ.get(
        "AAR_SNAP_CONFIG",
        str(Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
            / "agent-audio-relay" / "aar-snap.json"),
    )
)


def _load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        sys.stderr.write(
            f"aar-snap: config not found: {CONFIG_PATH}\n"
            "see module header for example schema; minimum is "
            "`servers`, `streams`, optionally `rooms`.\n"
        )
        sys.exit(2)
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (OSError, ValueError) as e:
        sys.stderr.write(f"aar-snap: config parse error ({CONFIG_PATH}): {e}\n")
        sys.exit(2)


def _rpc(server_url: str, method: str, params: dict[str, Any] | None = None) -> Any:
    """Call a Snapcast JSON-RPC method, return the unwrapped `result`."""
    body = {"id": 1, "jsonrpc": "2.0", "method": method}
    if params is not None:
        body["params"] = params
    req = urllib.request.Request(
        server_url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            resp = json.loads(r.read().decode())
    except urllib.error.URLError as e:
        raise SystemExit(f"aar-snap: {server_url} unreachable: {e}")
    if "error" in resp:
        raise SystemExit(f"aar-snap: rpc error: {resp['error']}")
    return resp.get("result")


def _resolve_channel(cfg: dict, name: str) -> tuple[str, str]:
    """Return (server_url, stream_id) for a channel name."""
    chan = cfg.get("streams", {}).get(name)
    if chan is None:
        raise SystemExit(
            f"aar-snap: unknown channel: {name} "
            f"(known: {', '.join(cfg.get('streams', {}))})"
        )
    server = chan["server"]
    server_url = cfg["servers"].get(server)
    if server_url is None:
        raise SystemExit(f"aar-snap: channel '{name}' references unknown server '{server}'")
    return server_url, chan["stream"]


def _find_client_anywhere(cfg: dict, room: str) -> tuple[str, str, dict]:
    """Search all configured servers for a client matching `room`. `room`
    can be either a key from cfg.rooms or the client's snapcast id/name.
    Returns (server_url, client_id, client_data).
    """
    # Resolve room alias to (server, client-name-or-id).
    alias = cfg.get("rooms", {}).get(room)
    if alias is not None:
        target_server = cfg["servers"][alias["server"]]
        target_name = alias["client"]
    else:
        target_server = None
        target_name = room
    for server_name, server_url in cfg["servers"].items():
        if target_server is not None and target_server != server_url:
            continue
        try:
            status = _rpc(server_url, "Server.GetStatus")
        except SystemExit:
            continue
        for group in status["server"]["groups"]:
            for client in group["clients"]:
                if (
                    client["id"] == target_name
                    or client["config"].get("name") == target_name
                    or client["host"].get("name") == target_name
                ):
                    return server_url, client["id"], client
    raise SystemExit(f"aar-snap: no client matching '{room}' on any configured server")


def _client_group_id(server_url: str, client_id: str) -> str:
    status = _rpc(server_url, "Server.GetStatus")
    for group in status["server"]["groups"]:
        for client in group["clients"]:
            if client["id"] == client_id:
                return group["id"]
    raise SystemExit(f"aar-snap: client {client_id} not found on {server_url}")


def cmd_list(_args, cfg: dict) -> int:
    for sname, surl in cfg["servers"].items():
        print(f"server {sname} ({surl})")
        try:
            st = _rpc(surl, "Server.GetStatus")
        except SystemExit as e:
            print(f"  unreachable: {e}")
            continue
        streams = {s["id"]: s for s in st["server"]["streams"]}
        print(f"  streams: {', '.join(streams) or '(none)'}")
        for group in st["server"]["groups"]:
            print(f"  group {group['id'][:8]} → stream={group['stream_id']}")
            for c in group["clients"]:
                v = c["config"]["volume"]
                name = c["config"].get("name") or c["host"].get("name") or c["id"]
                connected = "·" if c["connected"] else "✗"
                print(f"    {connected} {name:<20} vol={v['percent']:>3}{' (mute)' if v['muted'] else ''}")
    return 0


def cmd_rooms(_args, cfg: dict) -> int:
    print(f"{'ROOM':<14} {'CHANNEL':<14} {'VOL':>3}  {'STATE'}")
    seen: set[str] = set()
    # First, anything aliased in cfg.rooms
    for room_name, alias in cfg.get("rooms", {}).items():
        try:
            surl, cid, c = _find_client_anywhere(cfg, room_name)
        except SystemExit:
            print(f"{room_name:<14} (unreachable)")
            continue
        gid = _client_group_id(surl, cid)
        st = _rpc(surl, "Server.GetStatus")
        stream = next((g["stream_id"] for g in st["server"]["groups"] if g["id"] == gid), "?")
        # Find the channel name that matches (server, stream)
        chan = next(
            (cn for cn, cv in cfg.get("streams", {}).items()
             if cv["stream"] == stream and cfg["servers"].get(cv["server"]) == surl),
            stream,
        )
        v = c["config"]["volume"]
        state = "play" if c["connected"] else "off"
        if v["muted"]:
            state = "mute"
        print(f"{room_name:<14} {chan:<14} {v['percent']:>3}  {state}")
        seen.add(c["id"])
    return 0


def cmd_streams(_args, cfg: dict) -> int:
    for cname, cv in cfg.get("streams", {}).items():
        surl = cfg["servers"].get(cv["server"])
        print(f"{cname:<14} server={cv['server']:<6} stream={cv['stream']}  {surl}")
    return 0


def cmd_join(args, cfg: dict) -> int:
    chan_url, chan_stream = _resolve_channel(cfg, args.channel)
    surl, cid, _ = _find_client_anywhere(cfg, args.room)
    if surl != chan_url:
        # Snapcast clients can only listen to streams on the server they
        # connect to. Cross-server "join" doesn't make sense; user needs
        # a different snapclient process per server (which our setup has).
        raise SystemExit(
            f"aar-snap: room '{args.room}' is connected to a different "
            f"snapserver than channel '{args.channel}'. "
            "Use a per-server room name, or run a snapclient pointed at "
            "the right server."
        )
    gid = _client_group_id(surl, cid)
    _rpc(surl, "Group.SetStream", {"id": gid, "stream_id": chan_stream})
    print(f"aar-snap: {args.room} → {args.channel}")
    return 0


def cmd_volume(args, cfg: dict) -> int:
    pct = max(0, min(100, int(args.percent)))
    surl, cid, c = _find_client_anywhere(cfg, args.room)
    cur = c["config"]["volume"]
    _rpc(surl, "Client.SetVolume",
         {"id": cid, "volume": {"muted": cur["muted"], "percent": pct}})
    print(f"aar-snap: {args.room} volume → {pct}")
    return 0


def cmd_mute(args, cfg: dict) -> int:
    surl, cid, c = _find_client_anywhere(cfg, args.room)
    cur = c["config"]["volume"]
    if args.state == "on":
        new_muted = True
    elif args.state == "off":
        new_muted = False
    else:
        new_muted = not cur["muted"]
    _rpc(surl, "Client.SetVolume",
         {"id": cid, "volume": {"muted": new_muted, "percent": cur["percent"]}})
    print(f"aar-snap: {args.room} mute → {'on' if new_muted else 'off'}")
    return 0


def main() -> int:
    cfg = _load_config()
    p = argparse.ArgumentParser(prog="aar-snap", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="full state across servers").set_defaults(func=cmd_list)
    sub.add_parser("rooms", help="short room/channel/volume table").set_defaults(func=cmd_rooms)
    sub.add_parser("streams", help="list channels and their backing streams").set_defaults(func=cmd_streams)

    sj = sub.add_parser("join", help="route a room to a channel")
    sj.add_argument("channel")
    sj.add_argument("room")
    sj.set_defaults(func=cmd_join)

    sv = sub.add_parser("volume", help="set room volume (0-100)")
    sv.add_argument("room")
    sv.add_argument("percent", type=int)
    sv.set_defaults(func=cmd_volume)

    sm = sub.add_parser("mute", help="set or toggle mute on a room")
    sm.add_argument("room")
    sm.add_argument("state", nargs="?", choices=["on", "off"], default=None)
    sm.set_defaults(func=cmd_mute)

    args = p.parse_args()
    return args.func(args, cfg)


if __name__ == "__main__":
    sys.exit(main())
