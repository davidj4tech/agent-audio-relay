# agent-audio-relay

Deliver coding-agent TTS audio to your phone. A watcher daemon monitors
directories for new audio files and plays them on your phone via SSH +
`termux-media-player`. Hook scripts generate speech from Claude Code, Codex,
OpenCode, and Home Assistant agent responses using Edge TTS.

```
Claude Code ─(Stop hook)──► /tmp/tts-claude/voice-*.mp3
Codex       ─(stdin hook)─► /tmp/tts-codex/voice-*.mp3
OpenCode    ─(poll loop)──► /tmp/tts-opencode/voice-*.mp3
HA/openclaw ─(SSE stream)─► /tmp/openclaw/tts-ha/voice-*.opus
                                    │
                         ┌──────────┘
                         ▼
                agent-audio-relay (inotifywait)
                         │  queue → pad silence → scp → play
                         ▼
                phone (termux-media-player)
```

## Install

```sh
pip install --user agent-audio-relay
# or from source:
pip install --user /path/to/agent-audio-relay
```

## Components

### Watcher daemon (core)

The `agent-audio-relay` command watches directories for new
`tts-*/voice-*` audio files, queues them, pads 1s of silence (avoids
Edge TTS last-word clipping), SCPs to the phone, and plays via
`termux-media-player`. Back-to-back messages are sequenced — it waits
for current playback to finish before starting the next.

**Requirements:** `inotify-tools`, `ffmpeg`, SSH access to phone
(key auth, `termux-media-player` installed).

```sh
agent-audio-relay
```

| Variable | Default | Meaning |
|---|---|---|
| `RELAY_PHONE_HOST` | `p8ar` | SSH alias for the phone |
| `RELAY_PHONE_DEST` | `.cache/mel-latest` | Remote path prefix |
| `RELAY_WATCH_DIRS` | `/tmp/openclaw:/tmp` | Colon-separated dirs to watch |
| `RELAY_QUEUE_DIR` | `/tmp/agent-audio-relay-queue` | Local queue directory |
| `RELAY_MAX_RETRIES` | `2` | SCP/play retry count |
| `RELAY_MAX_PLAYBACK_WAIT` | `120` | Max seconds to wait for playback |

### Claude Code hook

Shell script registered as a Claude Code Stop hook. Extracts the last
assistant message from the conversation transcript, strips markdown,
generates Edge TTS audio, and drops it for the watcher.

```sh
# Install the hook
cp hooks/claude-code-tts-hook.sh ~/.claude/claude-tts-hook.sh
chmod +x ~/.claude/claude-tts-hook.sh
```

Then add to `~/.claude/settings.json`:

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

### Codex hook

Shell script for the OpenAI Codex CLI. Codex pipes the assistant response
text into the hook on stdin; the hook strips markdown, generates Edge TTS
audio, and drops it for the watcher.

```sh
cp hooks/codex-tts-hook.sh ~/.codex/codex-tts-hook.sh
chmod +x ~/.codex/codex-tts-hook.sh
```

| Variable | Default | Meaning |
|---|---|---|
| `CODEX_TTS_ENABLED` | `1` | Set to `0` to disable |
| `CODEX_TTS_VOICE` | `en-US-AriaNeural` | Edge TTS voice |
| `CODEX_TTS_DROP_DIR` | `/tmp/tts-codex` | Audio drop directory |

### OpenCode hook

Long-running watcher that polls OpenCode sessions for new `final_answer`
messages. Run as a systemd service alongside the main watcher.

```sh
# Install the service
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

### Home Assistant bridge

Listens to the HA SSE event stream for `openclaw_message_received`
events. Useful when running an openclaw agent through HA Assist.

```sh
HA_TOKEN="your-long-lived-token" hooks/ha-tts-bridge.sh
```

| Variable | Default | Meaning |
|---|---|---|
| `HA_URL` | `http://127.0.0.1:8123` | Home Assistant URL |
| `HA_TOKEN` | *(required)* | Long-lived access token |
| `TTS_VOICE` | `en-GB-SoniaNeural` | Edge TTS voice |

## systemd setup

```sh
mkdir -p ~/.config/systemd/user

# Main watcher
cp systemd/agent-audio-relay.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now agent-audio-relay

# OpenCode watcher (optional)
cp systemd/opencode-tts-watcher.service ~/.config/systemd/user/
# Edit ExecStart to point at your hooks/ path
systemctl --user daemon-reload
systemctl --user enable --now opencode-tts-watcher
```

## How it works

1. A coding agent (Claude Code, Codex, OpenCode, HA/openclaw) finishes responding
2. Its hook generates Edge TTS audio and drops it as `tts-<tool>/voice-<timestamp>.mp3`
3. The watcher daemon detects the new file via inotifywait
4. It copies the file into a queue, pads 1s of silence with ffmpeg
5. It waits for any current playback to finish on the phone
6. It SCPs the file to the phone and plays it via `termux-media-player`

The pad step prevents Edge TTS / termux-media-player from clipping the
last word of each message. The queue ensures back-to-back messages play
in order.

## Adding a new agent

To add TTS for any new tool, you just need a script that:

1. Detects when the tool finishes responding
2. Extracts the response text
3. Generates audio via `edge-tts --text "..." --write-media /tmp/tts-<tool>/voice-<timestamp>.mp3`

The watcher will pick it up automatically as long as the file matches
the `tts-*/voice-*` pattern under one of the watched directories.

## License

MIT
