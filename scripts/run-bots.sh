#!/usr/bin/env bash
# Launch the wolfbot Master and all configured NPC bots inside a single
# tmux session, one window per process.  Designed for macOS (and Linux)
# but tested primarily on macOS with iTerm2 + Homebrew tmux.
#
# Usage:
#   scripts/run-bots.sh                # auto-detect personas from envs/npc/.env.*
#   scripts/run-bots.sh setsu gina sq  # only the listed personas
#   scripts/run-bots.sh --mock         # offline mock mode (no LLM API calls,
#                                      # no real VOICEVOX needed beyond local
#                                      # daemon, fast phase durations)
#   scripts/run-bots.sh --mock setsu   # mock mode + persona filter
#   FORCE=1 scripts/run-bots.sh        # kill & recreate a stale session
#
# Mock mode injects these env vars into every spawned bot, overriding the
# values in the user's real .env.master / envs/npc/.env.<persona> via
# pydantic-settings precedence (process env beats .env file):
#   GAMEPLAY_LLM_PROVIDER=mock         # Master gameplay LLM → mock decider
#   NPC_LLM_PROVIDER=mock              # NPC speech LLM → scripted phrases
#   WOLFBOT_PHASE_DURATION_FACTOR=0.1  # phases 10x faster (60s vote → 6s)
# So the user keeps their real Discord token / VOICEVOX URL etc. and only
# the LLM and pacing layers swap to deterministic test stand-ins.
#
# After the session is up:
#   tmux attach -t wolfbot             # open the session
#   prefix + n / p                     # next/prev window
#   prefix + 0..9 / w                  # jump by index / pick from list
#   prefix + d                         # detach (bots keep running)
#
# Stop everything:  scripts/stop-bots.sh

set -euo pipefail

# ─── parse --mock flag (must come before persona args) ────────────────────
MOCK_MODE=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --mock)
            MOCK_MODE=1
            shift
            ;;
        --)
            shift
            break
            ;;
        -*)
            echo "ERROR: unknown flag: $1"
            echo "Usage: $0 [--mock] [persona ...]"
            exit 1
            ;;
        *)
            break
            ;;
    esac
done

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

if [[ ! -x "${REPO_ROOT}/.venv/bin/wolfbot" || ! -x "${REPO_ROOT}/.venv/bin/wolfbot-npc" ]]; then
    echo "ERROR: wolfbot entry points not found in ${REPO_ROOT}/.venv/bin/."
    echo "       Run 'uv sync' first to create the project venv."
    exit 1
fi

if [[ ! -f "${REPO_ROOT}/.env.master" ]]; then
    echo "ERROR: ${REPO_ROOT}/.env.master is missing."
    echo "       Copy .env.master.example, fill the secrets, and rerun."
    exit 1
fi

# ─── VOICEVOX engine reachability probe (warn, don't block) ───────────────
# In reactive_voice mode every NPC bot needs the VOICEVOX HTTP engine on
# its NPC_LLM_VOICEVOX_URL (default localhost:50021). Probing once before
# launch saves debugging time when the engine wasn't started — NPCs
# would otherwise spin up, register on the WS, and fail silently the
# moment a SpeakRequest arrives.
VOICEVOX_URL="${WOLFBOT_VOICEVOX_URL:-http://localhost:50021}"
if command -v curl >/dev/null 2>&1; then
    if ! curl -fsS --max-time 2 "${VOICEVOX_URL}/version" >/dev/null 2>&1; then
        echo "WARNING: VOICEVOX engine not reachable at ${VOICEVOX_URL}/version"
        echo "         NPCs will register with Master but fail on the first SpeakRequest."
        echo "         Start it before NPCs are dispatched. Examples:"
        echo "           macOS app:    open -a VOICEVOX"
        echo "           Docker:       docker run --rm -p 50021:50021 voicevox/voicevox_engine:cpu-latest"
        echo "         Override the probe target via WOLFBOT_VOICEVOX_URL=..."
    fi
fi

# We invoke .venv/bin/wolfbot* directly rather than `uv run` so that the
# project's pinned Python 3.11 is used regardless of the user's shell
# environment. UV_PYTHON / VIRTUAL_ENV pointing elsewhere (3.12 / 3.14
# system pythons) is common on multi-project machines and would cause
# `uv run` to error out with "incompatible interpreter".
WOLFBOT_BIN="${REPO_ROOT}/.venv/bin/wolfbot"
WOLFBOT_NPC_BIN="${REPO_ROOT}/.venv/bin/wolfbot-npc"

# ─── mock-mode env injection ──────────────────────────────────────────────
# Built once, then prefixed to every bot's command line so it shows up as
# real OS env at process start. pydantic-settings reads OS env *before*
# the .env file, so these values cleanly override whatever the user has
# in .env.master / envs/npc/.env.<persona>.
MOCK_ENV_PREFIX=""
if [[ "${MOCK_MODE}" == "1" ]]; then
    # Each variable is double-quoted so a value with shell-special chars
    # would still be safe — there's nothing to interpolate today, but
    # this keeps the pattern future-proof.
    MOCK_ENV_PREFIX="\
GAMEPLAY_LLM_PROVIDER='mock' \
NPC_LLM_PROVIDER='mock' \
WOLFBOT_PHASE_DURATION_FACTOR='0.1' "
    echo "Mock mode ON — injecting GAMEPLAY_LLM_PROVIDER=mock, NPC_LLM_PROVIDER=mock, WOLFBOT_PHASE_DURATION_FACTOR=0.1"
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

launch_in_window "master" "${LOG_DIR}/master.log" "${MOCK_ENV_PREFIX}'${WOLFBOT_BIN}'" "no"

# Wait for Master to announce its WebSocket bind in the log. Polling with
# a TCP probe (e.g. /dev/tcp) would write bytes to the socket and trigger
# spurious EOFError tracebacks on the websockets server side; tailing the
# log is non-intrusive. Discord login + VC join takes ~5-10s, and any NPC
# bot that tries to connect before that gets ConnectionRefusedError
# without auto-retry, so we serialize the dependency here.
echo "Waiting for Master to announce 'master_ws_listening' (up to 60s)..."
WS_READY=0
for _ in $(seq 1 60); do
    if grep -q "master_ws_listening" "${LOG_DIR}/master.log" 2>/dev/null; then
        WS_READY=1
        break
    fi
    sleep 1
done
if [[ "${WS_READY}" != "1" ]]; then
    echo "WARNING: Master did not announce WS readiness within 60s."
    echo "         Check 'tail -f ${LOG_DIR}/master.log' for errors before launching NPCs."
    echo "         NPC bots will still be started but may need manual restart after Master."
else
    echo "Master WS is ready."
fi

for persona in "${PERSONAS[@]}"; do
    launch_in_window \
        "${persona}" \
        "${LOG_DIR}/${persona}.log" \
        "${MOCK_ENV_PREFIX}WOLFBOT_NPC_ENV=envs/npc/.env.${persona} '${WOLFBOT_NPC_BIN}'"
    # Small stagger so 9 NPCs don't all finish Discord login and slam
    # Master's WS accept() in the same instant. Master + the NPC retry
    # loop both tolerate concurrency, but staggering avoids the easy
    # ECONNREFUSED race entirely.
    sleep 0.4
done

# Land on the master window when the user attaches.
tmux select-window -t "${SESSION}:master"

cat <<EOF

✅ tmux session '${SESSION}' is up$( [[ "${MOCK_MODE}" == "1" ]] && echo " (MOCK MODE)" ).
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
