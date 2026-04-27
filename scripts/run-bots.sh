#!/usr/bin/env bash
# Launch the wolfbot Master and all configured NPC bots inside a single
# tmux session, one window per process.  Designed for macOS (and Linux)
# but tested primarily on macOS with iTerm2 + Homebrew tmux.
#
# Usage:
#   scripts/run-bots.sh                # auto-detect personas from envs/npc/.env.*
#   scripts/run-bots.sh setsu gina sq  # only the listed personas
#   FORCE=1 scripts/run-bots.sh        # kill & recreate a stale session
#
# After the session is up:
#   tmux attach -t wolfbot             # open the session
#   prefix + n / p                     # next/prev window
#   prefix + 0..9 / w                  # jump by index / pick from list
#   prefix + d                         # detach (bots keep running)
#
# Stop everything:  scripts/stop-bots.sh

set -euo pipefail

# ─── locate repo root (parent of this script's dir) ───────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

SESSION="${WOLFBOT_TMUX_SESSION:-wolfbot}"
LOG_DIR="${REPO_ROOT}/logs"
mkdir -p "${LOG_DIR}"

# ─── prerequisites ────────────────────────────────────────────────────────
if ! command -v tmux >/dev/null 2>&1; then
    echo "ERROR: tmux is not installed."
    echo "       macOS: brew install tmux"
    echo "       Linux: apt install tmux  (or your distro equivalent)"
    exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
    echo "ERROR: uv is not installed (https://docs.astral.sh/uv/)."
    exit 1
fi

if [[ ! -f "${REPO_ROOT}/.env.master" ]]; then
    echo "ERROR: ${REPO_ROOT}/.env.master is missing."
    echo "       Copy .env.master.example, fill the secrets, and rerun."
    exit 1
fi

# ─── work out which NPC personas to launch ────────────────────────────────
declare -a PERSONAS=()
if [[ $# -gt 0 ]]; then
    PERSONAS=("$@")
else
    # Auto-detect from envs/npc/.env.<persona> (real, non-example files only).
    while IFS= read -r -d '' env_file; do
        base="$(basename "${env_file}")"
        # Strip the `.env.` prefix.  Skip *.example templates.
        persona="${base#.env.}"
        [[ "${persona}" == *.example ]] && continue
        PERSONAS+=("${persona}")
    done < <(find "${REPO_ROOT}/envs/npc" -maxdepth 1 -type f -name '.env.*' -print0 | sort -z)
fi

if [[ ${#PERSONAS[@]} -eq 0 ]]; then
    echo "ERROR: no NPC env files found under envs/npc/."
    echo "       Copy a template (e.g. envs/npc/.env.setsu.example → envs/npc/.env.setsu),"
    echo "       fill the secrets, and rerun."
    echo "       Or pass persona keys explicitly: scripts/run-bots.sh setsu gina sq"
    exit 1
fi

# Validate that every requested persona has a real env file.
for persona in "${PERSONAS[@]}"; do
    env_path="envs/npc/.env.${persona}"
    if [[ ! -f "${env_path}" ]]; then
        echo "ERROR: ${env_path} not found."
        echo "       cp envs/npc/.env.${persona}.example envs/npc/.env.${persona}"
        echo "       and fill in the secrets, then rerun."
        exit 1
    fi
done

# ─── handle existing session ──────────────────────────────────────────────
if tmux has-session -t "${SESSION}" 2>/dev/null; then
    if [[ "${FORCE:-0}" == "1" ]]; then
        echo "Killing existing tmux session '${SESSION}' (FORCE=1)."
        tmux kill-session -t "${SESSION}"
    else
        echo "tmux session '${SESSION}' is already running."
        echo "Attach with:  tmux attach -t ${SESSION}"
        echo "Recreate with: FORCE=1 scripts/run-bots.sh"
        exit 0
    fi
fi

# ─── helper to wrap a long-running command with logging + hold-on-exit ────
# `tmux send-keys` is used so each window can be re-entered later.  The
# trailing `; echo …; exec bash` keeps the pane open after the process
# exits, surfacing the failure log so the user can read it before manual
# teardown.
launch_in_window() {
    local window_name="$1"
    local log_file="$2"
    local cmd="$3"
    local create_new="${4:-yes}"

    if [[ "${create_new}" == "yes" ]]; then
        tmux new-window -t "${SESSION}" -n "${window_name}" -c "${REPO_ROOT}"
    else
        tmux rename-window -t "${SESSION}:0" "${window_name}"
    fi
    # `2>&1 | tee` so logs stream both to the pane and the file.
    tmux send-keys -t "${SESSION}:${window_name}" \
        "echo '── ${window_name} starting at $(date) ──' && ${cmd} 2>&1 | tee '${log_file}'; echo; echo '── ${window_name} exited (rc=${PIPESTATUS[0]:-?}) ──'; exec bash" \
        C-m
}

# ─── create the session, with the master in window 0 ──────────────────────
echo "Starting tmux session '${SESSION}' with 1 master + ${#PERSONAS[@]} NPC bot(s)..."
tmux new-session -d -s "${SESSION}" -c "${REPO_ROOT}"

launch_in_window "master" "${LOG_DIR}/master.log" "uv run wolfbot" "no"

for persona in "${PERSONAS[@]}"; do
    launch_in_window \
        "${persona}" \
        "${LOG_DIR}/${persona}.log" \
        "WOLFBOT_NPC_ENV=envs/npc/.env.${persona} uv run wolfbot-npc"
done

# Land on the master window when the user attaches.
tmux select-window -t "${SESSION}:master"

cat <<EOF

✅ tmux session '${SESSION}' is up.
   Master log:  ${LOG_DIR}/master.log
   NPC logs:    ${LOG_DIR}/<persona>.log

Attach:        tmux attach -t ${SESSION}
List windows:  tmux list-windows -t ${SESSION}
Stop all:      scripts/stop-bots.sh

NPCs (${#PERSONAS[@]}):
EOF
for persona in "${PERSONAS[@]}"; do
    echo "   - ${persona}"
done
