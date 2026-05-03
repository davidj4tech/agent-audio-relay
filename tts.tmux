#!/usr/bin/env bash
# agent-audio-relay tmux plugin entry point.
#
# Discovered automatically by TPM (https://github.com/tmux-plugins/tpm)
# when this repo is installed as a tmux plugin:
#
#     set -g @plugin 'davidj4tech/agent-audio-relay'
#     run '~/.tmux/plugins/tpm/tpm'
#
# Sets up:
#   - `prefix T` key table with sub-bindings (t = popup, Space = toggle,
#     r = replay), all session-pinned via '#{session_name}'.
#   - status-right append showing live TTS progress (tts-status-line).
#
# Both depend on aar's CLI being installed (pip install agent-audio-relay)
# so `tts-ctl`, `tts-popup`, and `tts-status-line` are on PATH.
#
# Configurable from the user's tmux config BEFORE TPM's `run` line:
#
#     set -g @tts-prefix-key      T          # default: T (under the prefix)
#     set -g @tts-popup-width     22         # tmux popup width
#     set -g @tts-popup-height    3          # tmux popup height (3 = 1 row + border)
#     set -g @tts-popup-x         R          # popup x-anchor (R/C/M/<col>)
#     set -g @tts-popup-y         0          # popup y-anchor (S/M/P/<row>)
#     set -g @tts-status-line     on         # append tts-status-line to status-right
#     set -g @tts-status-interval 1          # status refresh seconds (auto-bumped if not lower)

set -eu

opt() {
    local val
    val=$(tmux show -gqv "$1" 2>/dev/null)
    [ -n "$val" ] && printf '%s' "$val" || printf '%s' "$2"
}

PREFIX_KEY=$(opt @tts-prefix-key      T)
POPUP_W=$(   opt @tts-popup-width     22)
POPUP_H=$(   opt @tts-popup-height    3)
POPUP_X=$(   opt @tts-popup-x         R)
POPUP_Y=$(   opt @tts-popup-y         0)
WANT_STATUS=$(opt @tts-status-line    on)
STATUS_INT=$( opt @tts-status-interval 1)

# Key table: `prefix <PREFIX_KEY>` enters the `tts` table for one keystroke.
tmux bind "$PREFIX_KEY" switch-client -T tts

# Inside the `tts` table:
#   t      → small key-capture popup pinned top-right (interactive control)
#   Space  → toggle play/pause for the current session (one-shot)
#   r      → replay the current session's latest clip (one-shot)
tmux bind -T tts t \
    display-popup -E -w "$POPUP_W" -h "$POPUP_H" -x "$POPUP_X" -y "$POPUP_Y" \
    "TTS_POPUP_SESSION='#{session_name}' tts-popup"
tmux bind -T tts Space run-shell "tts-ctl toggle '#{session_name}'"
tmux bind -T tts r     run-shell "tts-ctl replay '#{session_name}'"

# status-right integration: append tts-status-line so a clip's progress is
# always visible in the status bar while playing, and invisible when idle.
if [ "$WANT_STATUS" != "off" ]; then
    # Bump status-interval down if the user's current value is higher; this
    # makes the progress bar advance smoothly. Don't *raise* it though.
    cur_interval=$(tmux show -gqv status-interval 2>/dev/null || echo "")
    if [ -z "$cur_interval" ] || [ "$cur_interval" -gt "$STATUS_INT" ] 2>/dev/null; then
        tmux set -g status-interval "$STATUS_INT"
    fi
    tmux set -ag status-right '#(tts-status-line) '
fi
