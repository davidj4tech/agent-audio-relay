"""Duck Snapcast music groups during TTS playback.

When the watcher delivers a TTS clip, we want any music currently
playing through Snapcast (mel `aar-music` stream, sp4r `aar-music`,
etc.) to drop in volume so the speech is audible, then restore. This
is producer-side ducking — Snapcast exposes per-client volume via
JSON-RPC, so we just call `Client.SetVolume` on every client whose
group is currently subscribed to a music stream.

The watcher calls `duck()` before the first clip in a queue and
`restore()` after the last clip finishes. Ref-counted so overlapping
calls (queued markers, retries, etc.) don't unduck mid-speech.

Configuration via env vars:
    RELAY_SNAPCAST_SERVERS — comma-separated `host:port` pairs of
        snapserver HTTP control endpoints (default: empty = no ducking).
        Example: `mel:1780,sp4:1780`.
    RELAY_SNAPCAST_DUCK_PERCENT — target relative volume during speech,
        as a percentage of pre-duck. Default: 30 (i.e. drop to 30%).
    RELAY_SNAPCAST_MUSIC_STREAMS — comma-separated stream IDs to duck.
        Default: `aar-music` (matches our setup; both mel + sp4r have
        identically-named streams).

If env is unset (or no servers reachable), the ducker is a no-op —
the watcher continues working without touching Snapcast.
"""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from typing import Iterable


def _env_list(name: str, default: str = "") -> list[str]:
    raw = os.environ.get(name, default).strip()
    return [x.strip() for x in raw.split(",") if x.strip()]


class SnapcastDucker:
    """Ref-counted volume ducker for Snapcast clients on music streams.

    Lifecycle:
        d = SnapcastDucker.from_env()
        d.duck()        # → drops volume on every client in a music group
        ...
        d.restore()     # → restores. Nested duck/restore are safe;
                        #   only the outermost restore actually un-ducks.
    """

    @classmethod
    def from_env(cls) -> "SnapcastDucker":
        servers = _env_list("RELAY_SNAPCAST_SERVERS")
        try:
            duck_pct = int(os.environ.get("RELAY_SNAPCAST_DUCK_PERCENT", "30"))
        except ValueError:
            duck_pct = 30
        streams = _env_list("RELAY_SNAPCAST_MUSIC_STREAMS", "aar-music")
        return cls(servers=servers, duck_percent=duck_pct, music_streams=streams)

    def __init__(
        self,
        servers: Iterable[str],
        duck_percent: int = 30,
        music_streams: Iterable[str] = ("aar-music",),
    ) -> None:
        # Each server: "host:port" — we POST JSON-RPC to /jsonrpc.
        self.servers = [s for s in servers]
        self.duck_percent = max(0, min(100, duck_percent))
        self.music_streams = set(music_streams)
        self._lock = threading.Lock()
        self._depth = 0
        # client_id → (server_url, pre_duck_volume_percent)
        self._saved: dict[str, tuple[str, int]] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.servers)

    def _rpc(self, server: str, method: str, params: dict | None = None) -> dict | None:
        url = f"http://{server}/jsonrpc"
        body: dict = {"id": 1, "jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=2) as r:
                resp = json.loads(r.read().decode())
        except (urllib.error.URLError, OSError, ValueError):
            return None
        return resp.get("result")

    def duck(self) -> None:
        """Drop volume on every client subscribed to a music stream.
        First call captures current volumes; nested calls bump a depth
        counter so the matching `restore()` doesn't unduck early.
        """
        if not self.enabled:
            return
        with self._lock:
            self._depth += 1
            if self._depth > 1:
                return  # already ducked
            for server in self.servers:
                status = self._rpc(server, "Server.GetStatus")
                if not status:
                    continue
                for group in status.get("server", {}).get("groups", []):
                    if group.get("stream_id") not in self.music_streams:
                        continue
                    for client in group.get("clients", []):
                        cid = client.get("id")
                        cfg = client.get("config", {}).get("volume", {})
                        if not cid or not cfg:
                            continue
                        pre = int(cfg.get("percent", 100))
                        # Only save first occurrence so concurrent music
                        # streams across two servers each get their own
                        # entry (cid is unique per snapserver, but the
                        # tuple is unique per (server, cid) — encode
                        # server in the key).
                        key = f"{server}::{cid}"
                        if key in self._saved:
                            continue
                        self._saved[key] = (server, pre)
                        new_pct = int(pre * self.duck_percent / 100)
                        self._rpc(server, "Client.SetVolume", {
                            "id": cid,
                            "volume": {"muted": cfg.get("muted", False),
                                       "percent": new_pct},
                        })

    def restore(self) -> None:
        """Restore volumes to what duck() captured. No-op if depth>0."""
        if not self.enabled:
            return
        with self._lock:
            if self._depth == 0:
                return
            self._depth -= 1
            if self._depth > 0:
                return
            saved = list(self._saved.items())
            self._saved.clear()
            for key, (server, pre) in saved:
                cid = key.split("::", 1)[1]
                # Don't read current volume — restore exactly to the
                # captured pre-duck percent. If a user manually changed
                # volume mid-duck, our restore overrides their change;
                # that's surprising in theory but exceedingly rare and
                # the alternative (best-effort merge) is a tarpit.
                self._rpc(server, "Client.SetVolume", {
                    "id": cid,
                    "volume": {"muted": False, "percent": pre},
                })
