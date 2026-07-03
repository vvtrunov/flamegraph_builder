#!/usr/bin/env bash
# =============================================================================
# profile_during_workload.sh — run multi_flamegraph only while a workload runs.
#
# Launches the profiler in the background, waits for the workload's loader process
# to appear in `ps`, then watches until it disappears and stops the profiler with a
# graceful SIGTERM (which triggers multi_flamegraph's normal merge + flamegraph
# finalize). Solves the "checkpointer never exits, so profiling never stops" problem.
# =============================================================================

set -uo pipefail

# ---------------------------------------------------------------------------
# Defaults (override via flags)
# ---------------------------------------------------------------------------
# Regexp matched (via pgrep -f) against each process's full command line.
PATTERN="/home/stroppy-postgres/stroppy/build/stroppy run"
POLL=2                 # seconds between `ps` checks
STARTUP_TIMEOUT=120    # seconds to wait for the workload to appear (0 = forever)

# Default profiler command (everything after `--` overrides this).
DEFAULT_PROFILER_CMD=(python3 -m multi_flamegraph
    --out ./prof
    --flamegraph-dir ~/FlameGraph/
    --profiler gdb
    --merge-all
    --config ../flamegraph_config.json)

usage() {
    cat << 'EOF'
USAGE
    profile_during_workload.sh [OPTIONS] [-- <profiler command ...>]

Runs the profiler (default: multi_flamegraph) for exactly as long as a workload
process is alive, then stops it gracefully so the flamegraphs are finalized.

OPTIONS
    --pattern <regexp>       Regexp (pgrep -f) identifying the workload's loader
                             process (default: the stroppy run path).
    --poll <sec>             Seconds between `ps` checks (default: 2).
    --startup-timeout <sec>  How long to wait for the workload to appear before
                             giving up; 0 = wait forever (default: 120).
    -- <cmd ...>             Profiler command to run. Defaults to:
                             python3 -m multi_flamegraph --out ./prof
                               --flamegraph-dir ~/FlameGraph/ --profiler gdb
                               --merge-all --config ../flamegraph_config.json
    --help, -h               Show this help.

FLOW
    1. Authenticate sudo up front (profiler needs root; backgrounding hides prompts).
    2. Start the profiler in the background.
    3. Wait for a process whose command line contains <pattern> to appear.
    4. Poll until no such process remains → send SIGTERM to the profiler.
    5. Wait for the profiler to finish finalizing and report its exit code.

    Press Ctrl-C to stop early: once = graceful finalize, twice = abort.
EOF
    exit 0
}

# ---------------------------------------------------------------------------
# Parse args
# ---------------------------------------------------------------------------
PROFILER_CMD=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --pattern)          PATTERN="$2"; shift 2 ;;
        --poll)             POLL="$2"; shift 2 ;;
        --startup-timeout)  STARTUP_TIMEOUT="$2"; shift 2 ;;
        -h|--help)          usage ;;
        --)                 shift; PROFILER_CMD=("$@"); break ;;
        *) echo "Error: unknown argument '$1' (use -- to pass a profiler command)." >&2
           exit 1 ;;
    esac
done
[[ ${#PROFILER_CMD[@]} -eq 0 ]] && PROFILER_CMD=("${DEFAULT_PROFILER_CMD[@]}")

if ! [[ "$POLL" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
    echo "Error: --poll must be a number (got: '$POLL')." >&2; exit 1
fi
if ! [[ "$STARTUP_TIMEOUT" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
    echo "Error: --startup-timeout must be a number (got: '$STARTUP_TIMEOUT')." >&2; exit 1
fi

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
# True (exit 0) if a workload process matching PATTERN (a regexp, matched against the
# full command line via pgrep -f) is alive — ignoring ourselves and the profiler.
workload_running() {
    local pid
    for pid in $(pgrep -f -- "$PATTERN"); do
        [[ "$pid" != "$$" && "$pid" != "${PROF_PID:-0}" ]] && return 0
    done
    return 1
}

PROF_PID=""
STOPPING=0
# Ctrl-C/SIGTERM on the wrapper: the profiler (same process group) also receives it
# directly and handles its own graceful/abort escalation, so we only note it here and
# fall through to waiting for the profiler to finish.
on_signal() {
    STOPPING=1
    echo ""
    echo "[wrapper] Stop signal received; waiting for the profiler to finalize ..."
}
trap on_signal INT TERM

profiler_alive() { [[ -n "$PROF_PID" ]] && kill -0 "$PROF_PID" 2>/dev/null; }

# ---------------------------------------------------------------------------
# 1. Authenticate sudo (so the backgrounded profiler never blocks on a prompt)
# ---------------------------------------------------------------------------
echo "[wrapper] Authenticating sudo (profiler attaches to processes as root) ..."
if ! sudo -v; then
    echo "Error: sudo authentication failed." >&2; exit 1
fi

# ---------------------------------------------------------------------------
# 2. Launch the profiler in the background (stdin from /dev/null so it never
#    tries to read the terminal; it stops on SIGTERM instead).
# ---------------------------------------------------------------------------
echo "[wrapper] Starting profiler: ${PROFILER_CMD[*]}"
"${PROFILER_CMD[@]}" < /dev/null &
PROF_PID=$!
echo "[wrapper] Profiler PID: $PROF_PID"

# ---------------------------------------------------------------------------
# 3. Wait for the workload to appear
# ---------------------------------------------------------------------------
echo "[wrapper] Waiting for workload matching: $PATTERN"
waited=0
until workload_running; do
    if ! profiler_alive; then
        echo "[wrapper] Profiler exited before the workload started." >&2
        wait "$PROF_PID"; exit $?
    fi
    if [[ "$STOPPING" -eq 1 ]]; then
        echo "[wrapper] Interrupted before workload appeared."
        break
    fi
    if [[ "$STARTUP_TIMEOUT" != "0" ]] && \
       (( $(echo "$waited >= $STARTUP_TIMEOUT" | bc -l) )); then
        echo "[wrapper] Workload did not appear within ${STARTUP_TIMEOUT}s; stopping profiler." >&2
        kill -TERM "$PROF_PID" 2>/dev/null || true
        wait "$PROF_PID"; exit 1
    fi
    sleep "$POLL"
    waited=$(echo "$waited + $POLL" | bc -l)
done
[[ "$STOPPING" -eq 0 ]] && echo "[wrapper] Workload detected; profiling in progress ..."

# ---------------------------------------------------------------------------
# 4. Watch until the workload is gone, then stop the profiler
# ---------------------------------------------------------------------------
while profiler_alive; do
    [[ "$STOPPING" -eq 1 ]] && break            # user asked to stop; profiler already signaled
    if ! workload_running; then
        echo "[wrapper] Workload finished; sending graceful stop to profiler."
        kill -TERM "$PROF_PID" 2>/dev/null || true
        break
    fi
    sleep "$POLL"
done

# ---------------------------------------------------------------------------
# 5. Wait for the profiler to finish finalizing
# ---------------------------------------------------------------------------
PROF_RC=0
while profiler_alive; do
    wait "$PROF_PID"; PROF_RC=$?          # re-wait if a trap interrupted the wait
done
echo "[wrapper] Profiler exited with code $PROF_RC."
exit "$PROF_RC"
