#!/bin/bash
# Shared helper for agent-audio-relay hooks: denote-style filename stems.
#
# Usage:
#   source "$(dirname "$0")/lib/denote-stem.sh"
#   stem=$(make_stem <agent> <kind> [session_override])
#
# Produces: YYYYMMDDTHHMMSS--<host>--<session>__<persona>_<agent>_<kind>
#
# - agent:  hook identifier (claude, codex, opencode, ha)
# - kind:   event type (stop, notif, announce, etc.)
# - host:   short hostname of the *producing* machine, slugged
# - session: explicit override; else tmux session name; else "nosession"
# - persona: $USER
#
# The host segment is encoded by the producer (not the relay) so two hosts
# with the same session name don't overwrite each other's symlinks on the
# playback host. Backends parse the host from this stem rather than calling
# gethostname() at archive time (which would always be the relay's hostname,
# useless for cross-host disambiguation).
#
# All components are sanitised to [A-Za-z0-9-] so the stem is safe as a
# filename on every target filesystem (incl. Android/Termux).

_denote_slug() {
    printf '%s' "$1" | tr -c 'A-Za-z0-9-' '-' | sed 's/-\+/-/g; s/^-//; s/-$//'
}

make_stem() {
    local agent="${1:?make_stem: agent required}"
    local kind="${2:?make_stem: kind required}"
    local session_override="${3:-}"

    local ts
    ts=$(date -u +%Y%m%dT%H%M%S)

    # Initialize locals to "" — tts-drop runs `set -u`, which would error on
    # `[ -z "$session" ]` if neither branch above assigned the variable.
    local session=""
    if [ -n "$session_override" ]; then
        session=$(_denote_slug "$session_override")
    elif [ -n "${TMUX_PANE:-}" ]; then
        session=$(tmux display-message -p -t "$TMUX_PANE" '#{session_name}' 2>/dev/null || true)
        session=$(_denote_slug "$session")
    fi
    [ -z "$session" ] && session="nosession"

    local persona=""
    persona=$(_denote_slug "${USER:-unknown}")
    [ -z "$persona" ] && persona="unknown"

    local host=""
    host=$(hostname -s 2>/dev/null | tr 'A-Z' 'a-z')
    host=$(_denote_slug "$host")
    [ -z "$host" ] && host="unknown"

    printf '%s--%s--%s__%s_%s_%s' "$ts" "$host" "$session" "$persona" "$(_denote_slug "$agent")" "$(_denote_slug "$kind")"
}
