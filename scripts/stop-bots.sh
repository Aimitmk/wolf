#!/usr/bin/env bash
# Cleanly tear down the wolfbot tmux session created by run-bots.sh.
#
# Each window holds two processes once the bot exits: the bot itself and
# the `exec bash` we left running so the log stays readable.  Killing the
# session takes both down in one go.

set -euo pipefail

SESSION="${WOLFBOT_TMUX_SESSION:-wolfbot}"

if ! command -v tmux >/dev/null 2>&1; then
    echo "ERROR: tmux is not installed."
    exit 1
fi

if tmux has-session -t "${SESSION}" 2>/dev/null; then
    echo "Stopping tmux session '${SESSION}'..."
    tmux kill-session -t "${SESSION}"
    echo "Done."
else
    echo "No tmux session named '${SESSION}' is running."
fi
