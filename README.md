# agent-audio-relay

Bidirectional voice interface for coding agents. Hooks capture agent
responses, generate TTS audio, and deliver it to a configurable playback
target. Input sources capture your voice, transcribe it, and route text
to the right agent.

```
         INPUT SOURCES                        OUTPUT HOOKS
  ┌─────────────────────────┐          ┌────────────────────────────┐
  │ HA Assist (earbuds/app) │          │ Claude Code (Stop hook)    │
  │ (future: local Whisper, │          │ Codex (stdin hook)         │
  │  Bluetooth PTT, WebRTC) │          │ OpenCode (session poller)  │
  └──────────┬──────────────┘          │ HA/openclaw (SSE bridge)   │
             │                         └──────────┬─────────────────┘
             ▼                                    │
     STT ──► router ──► agent pane                ▼
                                          tts-*/voice-*.mp3
                                                  │
                                    ┌─────────────┘
                                    ▼
                          agent-audio-relay (inotifywait)
                                    │  queue → pad silence → deliver
                                    ▼
                           PLAYBACK BACKENDS
                    ┌───────────────────────────┐
                    │ ssh-termux (SSH + phone)   │
                    │ mpv (local/IPC/remote)     │
                    │ (future: PipeWire, HTTP)   │
                    └───────────────────────────┘
```

## Install

```sh
pip install --user agent-audio-relay
# or from source:
pip install --user /path/to/agent-audio-relay
```

## Watcher daemon (core)

The `agent-audio-relay` command watches directories for new audio files
(`mp3`/`opus`/`ogg`/`wav`) dropped into any `tts-*` subdirectory, queues
them, optionally pads 1s of silence (avoids Edge TTS last-word
clipping), and delivers them through the configured playback backend.
Back-to-back messages are sequenced — it waits for current playback to
finish before starting the next.

Hooks name their clips with a denote-style stem
(`YYYYMMDDTHHMMSS--<session>__<persona>_<agent>_<kind>.<ext>`) via the
shared `hooks/lib/denote-stem.sh` helper, and the watcher preserves the
original stem end-to-end so backends can archive and replay by identity.

**Requirements:** `inotify-tools`, `ffmpeg` (for silence padding).

```sh
agent-audio-relay
```

| Variable | Default | Meaning |
|---|---|---|
| `RELAY_BACKEND` | `ssh-termux` | Default selector — bare backend or `backend:target` |
| `RELAY_CONTROL_FILE` | `/tmp/agent-audio-relay-backend` | Control file used by `switch` |
| `RELAY_PROFILES_FILE` | `~/.config/agent-audio-relay/profiles.json` | Alias map |
| `RELAY_WATCH_DIRS` | `/tmp/openclaw:/tmp` | Colon-separated dirs to watch |
| `RELAY_QUEUE_DIR` | `/tmp/agent-audio-relay-queue` | Local queue directory (set to `$XDG_RUNTIME_DIR/agent-audio-relay-queue` under systemd to avoid cross-user `/tmp` collisions — see the shipped unit) |
| `RELAY_PAD_SILENCE` | `1` | Pad 1s silence onto audio (`1` or `0`) |

### Switching targets on the fly

The daemon resolves its selector per audio file, so you can change
backend or output target from another shell without restarting:

```sh
agent-audio-relay switch mpv                         # whole-backend switch
agent-audio-relay switch ssh-termux:AA:BB:CC:DD:EE:FF # backend + target
agent-audio-relay switch headphones                  # alias from profiles.json
agent-audio-relay status                             # shows the active selector
agent-audio-relay list                               # prints backends + aliases
```

A selector has the form `<backend>[:<target>]` where `<target>` is
backend-specific — a BT MAC address for `ssh-termux` (requires
`RELAY_TERMUX_SWITCH_CMD` to actually reroute; see below), or a PipeWire
sink name for `mpv` (mapped to `--audio-device=pulse/<target>`).

### Aliases (`profiles.json`)

Define friendly names for selectors in
`~/.config/agent-audio-relay/profiles.json`:

```json
{
  "aliases": {
    "headphones": { "backend": "ssh-termux", "target": "AA:BB:CC:DD:EE:FF" },
    "car":        { "backend": "ssh-termux", "target": "11:22:33:44:55:66" },
    "speaker":    { "backend": "mpv",        "target": "bluez_sink.XX_XX_XX_XX_XX_XX.a2dp_sink" },
    "local":      { "backend": "mpv" },
    "phone":      { "backend": "ssh-termux" }
  }
}
```

See `examples/profiles.json` for a starter.

## Playback backends

### ssh-termux

Delivers audio to a remote device via SCP + `termux-media-player`. The
original backend — designed for Android phones running Termux over SSH.

| Variable | Default | Meaning |
|---|---|---|
| `RELAY_SSH_HOST` | `p8ar` | SSH alias for the target device |
| `RELAY_SSH_MAX_RETRIES` | `2` | Retry count for SCP/play |
| `RELAY_SSH_PLAYBACK_WAIT` | `120` | Max seconds to wait for playback |
| `RELAY_TERMUX_PLAYER` | `termux-media-player` | `mpv-ipc` to deliver via the long-running mpv-tts daemon (required if you want `tts-ctl`/popup controls to actually control delivery audio) |
| `RELAY_TERMUX_MPV_SOCK` | `/data/data/com.termux/files/usr/tmp/mpv-tts.sock` | Remote mpv IPC socket path |
| `RELAY_TERMUX_SWITCH_CMD` | *(empty)* | Remote command run before playing when the target changes — the target is appended as a shell-quoted arg. Unset means no reroute. |

#### Bluetooth target switching

Android doesn't expose a stable API for picking the active A2DP device
from Termux, so the relay delegates the actual routing to a user-supplied
command. Set `RELAY_TERMUX_SWITCH_CMD` to a shell invocation on the phone
that, when passed a target identifier as its last argument, makes that
device the active media sink. The target string is opaque to the relay —
it can be a MAC address, a Tasker task name, whatever your switch script
understands.

Rough examples (pick what fits your phone):

```sh
# Rooted phone — cmd bluetooth_manager connect to a MAC
export RELAY_TERMUX_SWITCH_CMD='su -c "cmd bluetooth_manager connect"'

# Tasker (AutoRemote / EventGhost-style) — fire an intent per target
export RELAY_TERMUX_SWITCH_CMD='am broadcast -a net.dinglisch.android.tasker.ACTION_TASK -e task_name BTSwitch --es par1'

# Your own wrapper on the phone
export RELAY_TERMUX_SWITCH_CMD='~/bin/bt-switch'
```

`switch` logs `BT:SWITCH <target>` on success, `BT:FAIL …` on a non-zero
exit, and `BT:SKIPPED (no RELAY_TERMUX_SWITCH_CMD configured)` when a
target was selected but no command is set — in which case playback still
goes to whichever device Android currently considers active.

Clips are archived on the phone under `~/.cache/agent-audio/<stem>.<ext>`.
The backend maintains three symlinks per clip:

- `latest.<ext>` — global most-recent
- `latest--<session>.<ext>` — most-recent from a given session
- `latest--<session>__<agent>.<ext>` — session + agent scoped

`bin/tts-ctl` uses those symlinks to implement session-aware replay.

#### SSH setup for Termux

The ssh-termux backend requires passwordless SSH access to an Android
device running [Termux](https://termux.dev/) with
[Termux:API](https://wiki.termux.com/wiki/Termux:API) installed. Here's
the setup from scratch.

**On the phone (Termux):**

```sh
# Install the SSH server and media player
pkg install openssh termux-api

# Start sshd (listens on port 8022 by default)
sshd

# Check your username — Termux uses a non-standard one
whoami
# Typically: u0_a317 or similar
```

**On the host (your server):**

```sh
# Copy your SSH key to the phone
# Replace <phone-ip> and <termux-user> with your values
ssh-copy-id -p 8022 <termux-user>@<phone-ip>

# Verify passwordless login works
ssh -p 8022 <termux-user>@<phone-ip> echo ok
```

**Create an SSH alias** in `~/.ssh/config` so the relay can connect by
name:

```sshconfig
Host phone
  HostName <phone-ip>
  Port 8022
  User <termux-user>
```

Then test end-to-end:

```sh
# Verify termux-media-player works
ssh phone termux-media-player info

# Set the relay to use your alias
export RELAY_SSH_HOST=phone
```

**Recommended: ControlMaster** for low-latency repeated connections.
Without it, every SCP + play cycle opens two new SSH connections. With
it, subsequent connections reuse the first one:

```sshconfig
Host phone
  HostName <phone-ip>
  Port 8022
  User <termux-user>
  ControlMaster auto
  ControlPath ~/.ssh/sockets/%r@%h-%p
  ControlPersist 600
```

```sh
mkdir -p ~/.ssh/sockets
```

**Tailscale** works well if the phone and server are on different
networks. The phone's Tailscale IP is stable, so the SSH alias doesn't
break when you move between Wi-Fi and mobile data.

**Troubleshooting:**

- `PLAY:FAILED (ssh)` — check `ssh phone echo ok` works non-interactively
- `PLAY:FAILED (scp)` — check disk space on the phone (`df -h` in Termux)
- Audio plays but is silent — check phone volume; `termux-volume` can help
- `termux-media-player: command not found` — install `termux-api` package
  *and* the Termux:API Android app from F-Droid

### mpv

Plays audio locally via mpv. Supports direct invocation (spawns mpv per
file) or IPC mode (sends commands to a running mpv instance via its JSON
IPC socket). IPC mode is useful for gapless sequencing or routing audio
to a specific output device.

```sh
# Direct mode
RELAY_BACKEND=mpv agent-audio-relay

# IPC mode — start mpv with a socket first:
mpv --idle --input-ipc-server=/tmp/mpv-relay.sock --audio-device=pulse/your-sink
# Then point the relay at it:
RELAY_BACKEND=mpv RELAY_MPV_SOCKET=/tmp/mpv-relay.sock agent-audio-relay
```

| Variable | Default | Meaning |
|---|---|---|
| `RELAY_MPV_BIN` | `mpv` | Path to mpv binary |
| `RELAY_MPV_SOCKET` | *(empty)* | IPC socket path (enables IPC mode) |
| `RELAY_MPV_ARGS` | *(empty)* | Extra mpv arguments (space-separated) |
| `RELAY_MPV_WAIT` | `1` | Wait for playback to finish (`1` or `0`) |

## Output hooks

These generate TTS audio from agent responses and drop files where the
watcher picks them up. Each hook is tailored to a specific agent's
interface.

### Claude Code

Shell script registered as a Claude Code Stop hook. Extracts the last
assistant message from the conversation transcript, strips markdown,
generates Edge TTS audio.

```sh
cp hooks/claude-code-tts-hook.sh ~/.claude/claude-tts-hook.sh
chmod +x ~/.claude/claude-tts-hook.sh
```

Register in `~/.claude/settings.json`:

```json
{
  "hooks": {
    "Stop": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "~/.claude/claude-tts-hook.sh",
            "timeout": 120
          }
        ]
      }
    ]
  }
}
```

| Variable | Default | Meaning |
|---|---|---|
| `CLAUDE_TTS_ENABLED` | `1` | Set to `0` to disable |
| `CLAUDE_TTS_ENGINE` | `edge` | TTS engine — `edge` (Microsoft Edge TTS, free) or `openai` (OpenAI TTS, paid, better voices) |
| `CLAUDE_TTS_VOICE` | `en-US-AriaNeural` (edge) / `marin` (openai) | Voice name. Edge uses names like `en-US-AriaNeural`, `en-AU-NatashaNeural`; OpenAI uses `alloy`, `marin`, `sage`, `nova`, etc. |
| `CLAUDE_TTS_DROP_DIR` | `/tmp/tts-claude` | Audio drop directory |
| `CLAUDE_TTS_OPENAI_MODEL` | `gpt-4o-mini-tts` | OpenAI TTS model (only used when engine=`openai`) |
| `CLAUDE_TTS_OPENAI_PYTHON` | `python3` | Python interpreter with the `openai` package installed (only used when engine=`openai`) |

When `CLAUDE_TTS_ENGINE=openai`, the hook needs `OPENAI_API_KEY` in its
environment (set it via the `env` block on the hook entry in
`~/.claude/settings.json`, or globally in your shell).

### Codex

Shell script for the OpenAI Codex CLI. Codex pipes assistant response
text into the hook on stdin.

```sh
cp hooks/codex-tts-hook.sh ~/.codex/codex-tts-hook.sh
chmod +x ~/.codex/codex-tts-hook.sh
```

| Variable | Default | Meaning |
|---|---|---|
| `CODEX_TTS_ENABLED` | `1` | Set to `0` to disable |
| `CODEX_TTS_VOICE` | `en-US-AriaNeural` | Edge TTS voice |
| `CODEX_TTS_DROP_DIR` | `/tmp/tts-codex` | Audio drop directory |

### Pi (pi-coding-agent)

[`pi`](https://github.com/badlogic/pi-mono) is a TypeScript coding-agent
harness with a first-class extension API rather than a shell-hook config,
so the integration is shipped as a TS extension instead of a shell
script. It subscribes to the `agent_end` lifecycle event, extracts the
final assistant text from `event.messages`, strips markdown, runs TTS,
and drops the audio into a watched directory like the other hooks.
Uses `fetch` directly for OpenAI TTS and spawns `edge-tts` for the
optional Edge engine — no extra npm dependencies.

```sh
mkdir -p ~/.pi/agent/extensions
cp extensions/pi-tts-extension.ts ~/.pi/agent/extensions/agent-audio-relay-tts.ts
# Reload pi or start a new session.
```

| Variable | Default | Meaning |
|---|---|---|
| `PI_TTS_ENABLED` | `1` | Set to `0` to disable |
| `PI_TTS_ENGINE` | `openai` | `openai` or `edge` |
| `PI_TTS_VOICE` | `marin` (openai) / `en-US-AriaNeural` (edge) | Voice name |
| `PI_TTS_OPENAI_MODEL` | `gpt-4o-mini-tts` | OpenAI TTS model |
| `PI_TTS_EDGE_BIN` | `edge-tts` | Path to `edge-tts` (engine=edge) |
| `PI_TTS_DROP_DIR` | `/tmp/tts-pi` | Audio drop directory |
| `PI_TTS_MAX_CHARS` | `4000` | Cap on text length sent to TTS |

When `PI_TTS_ENGINE=openai`, set `OPENAI_API_KEY` in the environment pi
is launched from.

### OpenCode

Long-running watcher that polls OpenCode sessions for new `final_answer`
messages. Run as a systemd service alongside the main watcher.

```sh
cp systemd/opencode-tts-watcher.service ~/.config/systemd/user/
# Edit ExecStart path, then:
systemctl --user daemon-reload
systemctl --user enable --now opencode-tts-watcher
```

| Variable | Default | Meaning |
|---|---|---|
| `OPENCODE_TTS_ENABLED` | `1` | Set to `0` to disable |
| `OPENCODE_TTS_VOICE` | `en-US-AriaNeural` | Edge TTS voice |
| `OPENCODE_TTS_DROP_DIR` | `/tmp/tts-opencode` | Audio drop directory |
| `OPENCODE_TTS_POLL_INTERVAL` | `3` | Seconds between polls |
| `OPENCODE_TTS_MAX_MESSAGE_AGE` | `300` | Skip messages older than this |

### HA/openclaw (SSE bridge)

Listens to the Home Assistant SSE event stream for
`openclaw_message_received` events. Generates TTS from openclaw agent
responses delivered through HA.

```sh
HA_TOKEN="your-long-lived-token" hooks/ha-tts-bridge.sh
```

| Variable | Default | Meaning |
|---|---|---|
| `HA_URL` | `http://127.0.0.1:8123` | Home Assistant URL |
| `HA_TOKEN` | *(required)* | Long-lived access token |
| `TTS_VOICE` | `en-GB-SoniaNeural` | Edge TTS voice |

## Input sources

Input sources capture your voice, transcribe it, and route the text to
a coding agent. Currently the only implemented input source is Home
Assistant Assist — see [tmux-voice-bridge](https://github.com/davidj4tech/tmux-voice-bridge)
for that piece.

Future input sources could include local Whisper + a push-to-talk daemon,
Bluetooth earbud button detection, or a web-based interface — replacing
the HA dependency for voice input with something lighter.

## systemd setup

```sh
mkdir -p ~/.config/systemd/user

# Make sure user services keep running after logout / across reboots
sudo loginctl enable-linger "$USER"

# Main watcher
cp systemd/agent-audio-relay.service ~/.config/systemd/user/
# Edit RELAY_BACKEND and backend-specific vars as needed
systemctl --user daemon-reload
systemctl --user reenable --now agent-audio-relay

# OpenCode watcher (optional)
cp systemd/opencode-tts-watcher.service ~/.config/systemd/user/
# Edit ExecStart to point at your hooks/ path
systemctl --user daemon-reload
systemctl --user reenable --now opencode-tts-watcher
```

## Playback control

`bin/tts-ctl` talks JSON-IPC to a long-running mpv daemon (the Termux
service `mpv-tts`, exposing `$PREFIX/tmp/mpv-tts.sock`). On the phone
the script writes to the socket directly; from any other host it
tunnels over `ssh $CLAUDE_TTS_PHONE_HOST` (default `p8ar`).

```sh
tts-ctl pause            # pause current playback
tts-ctl resume           # resume
tts-ctl toggle           # pause/resume depending on state
tts-ctl replay           # replay latest from the current tmux session
                         #   (falls back to global latest)
tts-ctl replay foo       # replay latest from session "foo"
tts-ctl seek 5           # relative seek (negative = backwards)
tts-ctl volume -5        # add to mpv volume
tts-ctl status           # state + position/duration + volume
tts-ctl nowplaying       # current file path
```

Required on the phone: `mpv`, `socat`, `jq`, and an mpv daemon launched
with `--input-ipc-server=$PREFIX/tmp/mpv-tts.sock --idle=yes`. Override
the socket with `MPV_TTS_SOCK` if your daemon binds elsewhere.

The companion project [mpv-mcp](https://github.com/davidj4tech/mpv-mcp)
(Node, runs in Termux) provisions both `mpv` channels as runit services,
exposes the same controls as an MCP server + HTTP/JSON API, and serves
an installable PWA at `http://<tailscale-ip>:8765/` with mpv-style
keyboard shortcuts. If you set `RELAY_TERMUX_PLAYER=mpv-ipc` on this
relay, that's the phone-side daemon you're talking to.

### Floating tmux popup

`bin/tts-popup` is an interactive single-key controller meant to run
inside `tmux display-popup -E`. Suggested bindings (in
`~/.tmux.conf.local`):

```tmux
bind T switch-client -T tts
bind -T tts t     display-popup -E -w 95% -h 7 "~/.local/bin/tts-popup"
bind -T tts Space display-popup -E -w 95% -h 3 "~/.local/bin/tts-ctl toggle"
bind -T tts r     display-popup -E -w 95% -h 3 "~/.local/bin/tts-ctl replay"
```

Hotkeys inside the interactive popup: `Space` toggle, `p` pause, `R`
resume, `r` replay, `h`/`l` seek ±5s, `H`/`L` seek ±30s, `-`/`=`
volume ±5, `q` (or `Esc`) close. Width is given as a percentage so
the popup fits on narrow displays (Termux on a phone).

## Adding a new agent hook

To add TTS for any new tool, write a script that:

1. Detects when the tool finishes responding
2. Extracts the response text
3. Sources `hooks/lib/denote-stem.sh` and names the clip via
   `make_stem <agent> <kind> [session_override]` so it carries
   session/persona identity end-to-end
4. Generates audio:
   `edge-tts --text "..." --write-media "/tmp/tts-<tool>/$(make_stem <tool> <kind>).mp3"`

The watcher picks up any supported audio file dropped into a `tts-*`
subdirectory under a watched path.

## Adding a new playback backend

Subclass `agent_audio_relay.backends.PlaybackBackend` and implement
`play(path)` and optionally `wait_for_playback()`. Register it in
`backends/registry.py`. See `ssh_termux.py` or `mpv.py` for examples.

## License

MIT
