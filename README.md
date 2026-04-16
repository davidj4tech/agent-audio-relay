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

The `agent-audio-relay` command watches directories for new
`tts-*/voice-*` audio files, queues them, optionally pads 1s of silence
(avoids Edge TTS last-word clipping), and delivers them through the
configured playback backend. Back-to-back messages are sequenced — it
waits for current playback to finish before starting the next.

**Requirements:** `inotify-tools`, `ffmpeg` (for silence padding).

```sh
agent-audio-relay
```

| Variable | Default | Meaning |
|---|---|---|
| `RELAY_BACKEND` | `ssh-termux` | Playback backend (`ssh-termux` or `mpv`) |
| `RELAY_WATCH_DIRS` | `/tmp/openclaw:/tmp` | Colon-separated dirs to watch |
| `RELAY_QUEUE_DIR` | `/tmp/agent-audio-relay-queue` | Local queue directory |
| `RELAY_PAD_SILENCE` | `1` | Pad 1s silence onto audio (`1` or `0`) |

## Playback backends

### ssh-termux

Delivers audio to a remote device via SCP + `termux-media-player`. The
original backend — designed for Android phones running Termux over SSH.

| Variable | Default | Meaning |
|---|---|---|
| `RELAY_SSH_HOST` | `p8ar` | SSH alias for the target device |
| `RELAY_SSH_DEST` | `.cache/relay-latest` | Remote path prefix for audio |
| `RELAY_SSH_MAX_RETRIES` | `2` | Retry count for SCP/play |
| `RELAY_SSH_PLAYBACK_WAIT` | `120` | Max seconds to wait for playback |

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
| `CLAUDE_TTS_VOICE` | `en-US-AriaNeural` | Edge TTS voice |
| `CLAUDE_TTS_DROP_DIR` | `/tmp/tts-claude` | Audio drop directory |

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

# Main watcher
cp systemd/agent-audio-relay.service ~/.config/systemd/user/
# Edit RELAY_BACKEND and backend-specific vars as needed
systemctl --user daemon-reload
systemctl --user enable --now agent-audio-relay

# OpenCode watcher (optional)
cp systemd/opencode-tts-watcher.service ~/.config/systemd/user/
# Edit ExecStart to point at your hooks/ path
systemctl --user daemon-reload
systemctl --user enable --now opencode-tts-watcher
```

## Adding a new agent hook

To add TTS for any new tool, write a script that:

1. Detects when the tool finishes responding
2. Extracts the response text
3. Generates audio: `edge-tts --text "..." --write-media /tmp/tts-<tool>/voice-<timestamp>.mp3`

The watcher picks it up automatically — any file matching `tts-*/voice-*`
under a watched directory gets queued and delivered.

## Adding a new playback backend

Subclass `agent_audio_relay.backends.PlaybackBackend` and implement
`play(path)` and optionally `wait_for_playback()`. Register it in
`backends/registry.py`. See `ssh_termux.py` or `mpv.py` for examples.

## License

MIT
