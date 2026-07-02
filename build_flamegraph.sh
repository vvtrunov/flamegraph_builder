#!/usr/bin/env bash
# =============================================================================
# pg_flamegraph.sh — record a CPU/off-CPU profile of a PostgreSQL backend and
#                    generate a flamegraph SVG.
# =============================================================================

set -uo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
PROFILER="perf"
FREQUENCY=99
# No hardcoded path. Inherit an exported $FLAMEGRAPH_DIR if present; otherwise it is
# resolved below (flamegraph.pl on $PATH, then ~/FlameGraph). --flamegraph-dir overrides.
FLAMEGRAPH_DIR="${FLAMEGRAPH_DIR:-}"
PID=""
OUTPUT_BASE=""
TIMEOUT=""

# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------
usage() {
    cat << 'EOF'
USAGE
    pg_flamegraph.sh --pid <pid> --output-dir <dir> [OPTIONS]


REQUIRED ARGUMENTS
    --pid, -p <pid>
        PID of the PostgreSQL backend process to profile.
        Must be a running process accessible to the current user.

    --output-dir, -o <dir>
        Base directory where profiling output will be stored.
        A timestamped subdirectory (YYYYMMDD_HHMMSS) is created inside it.
        The directory is created automatically if it does not exist.

OPTIONS
    --profiler <perf|gdb>       (default: perf)
        Backend to use for stack collection.

          perf  On-CPU profiling via linux-perf.
                Captures only time spent on-CPU.
                Requires: perf, FlameGraph toolkit.

          gdb   On+off-CPU stack sampling via gdb.
                Captures time regardless of CPU state — useful for profiling
                lock contention, I/O waits, and other off-CPU bottlenecks.
                Requires: gdb, FlameGraph toolkit (stackcollapse-gdb.pl).

    --frequency, -f <hz>        (default: 99)
        Sampling frequency in Hz.
        perf:  passed as -F to `perf record`.
        gdb:   sleep interval between samples = 1 / frequency.
        Lower values reduce overhead; higher values improve resolution.

    --timeout, -t <seconds>     (default: none)
        Record for a fixed duration, then stop automatically.
        When omitted, recording runs until you press ENTER.
        When set, recording auto-stops after <seconds>; pressing ENTER
        still stops it early. Useful for unattended/scripted profiling.

    --flamegraph-dir <path>     (default: auto-resolved)
        Path to a local clone of Brendan Gregg's FlameGraph toolkit.
        If omitted, resolved from $FLAMEGRAPH_DIR, then flamegraph.pl on
        $PATH, then ~/FlameGraph.
        Clone it with:
            git clone https://github.com/brendangregg/FlameGraph.git ~/FlameGraph

    --help, -h
        Show this help and exit.

OUTPUT FILES
    <output_dir>/<timestamp>/
        pg_perf.data        Raw perf data                    (perf only)
        pg_perf.out         Perf script output               (perf only)
        pg_gdb_raw.out      Raw gdb backtraces               (gdb only)
        pg_folded.out       Folded stacks (both backends)
        pg_flamegraph.svg   Final flamegraph                 (both backends)

EXAMPLES
    # On-CPU flamegraph using perf (default)
    pg_flamegraph.sh --pid 12345 --output-dir /home/user/profiles

    # Off-CPU capable flamegraph using gdb
    pg_flamegraph.sh --pid 12345 --output-dir /home/user/profiles --profiler gdb

    # Unattended capture: record for 30 seconds then stop automatically
    pg_flamegraph.sh --pid 12345 --output-dir /home/user/profiles --timeout 30

    # Higher resolution, custom FlameGraph path
    pg_flamegraph.sh --pid 12345 --output-dir /tmp/pg_prof \
                     --profiler gdb --frequency 200 \
                     --flamegraph-dir ~/tools/FlameGraph

EOF
    exit 0
}

# ---------------------------------------------------------------------------
# Args
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --pid|-p)
            PID="$2"; shift 2 ;;
        --output-dir|-o)
            OUTPUT_BASE="$2"; shift 2 ;;
        --profiler)
            PROFILER="$2"; shift 2 ;;
        --frequency|-f)
            FREQUENCY="$2"; shift 2 ;;
        --timeout|-t)
            TIMEOUT="$2"; shift 2 ;;
        --flamegraph-dir)
            FLAMEGRAPH_DIR="$2"; shift 2 ;;
        -h|--help)
            usage ;;
        --)
            shift; break ;;
        *)
            echo "Error: unknown argument '$1'" >&2
            echo "Run '$0 --help' for usage." >&2
            exit 1 ;;
    esac
done

# Validate required args
if [[ -z "$PID" || -z "$OUTPUT_BASE" ]]; then
    echo "Error: --pid and --output-dir are required." >&2
    echo "Run '$0 --help' for usage." >&2
    exit 1
fi

if ! [[ "$PROFILER" =~ ^(perf|gdb)$ ]]; then
    echo "Error: --profiler must be 'perf' or 'gdb' (got: '$PROFILER')." >&2
    exit 1
fi

if ! [[ "$PID" =~ ^[0-9]+$ ]]; then
    echo "Error: '$PID' is not a valid PID." >&2
    exit 1
fi

if ! kill -0 "$PID" 2>/dev/null; then
    echo "Error: No process found with PID $PID." >&2
    exit 1
fi

if ! [[ "$FREQUENCY" =~ ^[0-9]+([.][0-9]+)?$ ]] || (( $(echo "$FREQUENCY <= 0" | bc -l) )); then
    echo "Error: --frequency must be a positive number (got: '$FREQUENCY')." >&2
    exit 1
fi

if [[ -n "$TIMEOUT" ]]; then
    if ! [[ "$TIMEOUT" =~ ^[0-9]+([.][0-9]+)?$ ]] || (( $(echo "$TIMEOUT <= 0" | bc -l) )); then
        echo "Error: --timeout must be a positive number of seconds (got: '$TIMEOUT')." >&2
        exit 1
    fi
fi

# ---------------------------------------------------------------------------
# Resolve FlameGraph toolkit location (no hardcoded absolute path).
# Precedence: --flamegraph-dir > $FLAMEGRAPH_DIR > flamegraph.pl on $PATH > ~/FlameGraph
# ---------------------------------------------------------------------------
if [[ -z "$FLAMEGRAPH_DIR" ]]; then
    if fg_pl=$(command -v flamegraph.pl 2>/dev/null); then
        FLAMEGRAPH_DIR=$(dirname "$fg_pl")
    else
        FLAMEGRAPH_DIR="$HOME/FlameGraph"
    fi
fi

# ---------------------------------------------------------------------------
# Create timestamped output directory
# ---------------------------------------------------------------------------
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
OUT_DIR="${OUTPUT_BASE}/${TIMESTAMP}"
mkdir -p "$OUT_DIR"

# Shared output paths
PERF_DATA="${OUT_DIR}/pg_perf.data"
PERF_SCRIPT="${OUT_DIR}/pg_perf.out"
GDB_RAW="${OUT_DIR}/pg_gdb_raw.out"
FOLDED="${OUT_DIR}/pg_folded.out"
SVG="${OUT_DIR}/pg_flamegraph.svg"

echo "============================================="
echo "  PostgreSQL flamegraph profiler"
echo "============================================="
echo "  PID        : $PID"
echo "  Backend    : $PROFILER"
echo "  Frequency  : ${FREQUENCY} Hz"
if [[ -n "$TIMEOUT" ]]; then
    echo "  Duration   : ${TIMEOUT}s (auto-stop)"
else
    echo "  Duration   : until ENTER"
fi
echo "  Out dir    : $OUT_DIR"
echo "============================================="
echo ""

# ---------------------------------------------------------------------------
# Dependency checks
# ---------------------------------------------------------------------------
check_deps_perf() {
    if ! command -v perf &>/dev/null; then
        echo "Error: 'perf' not found. Install linux-tools or use --profiler gdb." >&2
        exit 1
    fi
    if [[ ! -d "$FLAMEGRAPH_DIR" ]]; then
        echo "Error: FlameGraph toolkit not found at $FLAMEGRAPH_DIR" >&2
        echo "       Clone it: git clone https://github.com/brendangregg/FlameGraph.git ~/FlameGraph" >&2
        echo "       Or pass a different path: --flamegraph-dir <path>" >&2
        exit 1
    fi
}

check_deps_gdb() {
    if ! command -v gdb &>/dev/null; then
        echo "Error: 'gdb' not found. Install gdb or use --profiler perf." >&2
        exit 1
    fi
    if [[ ! -d "$FLAMEGRAPH_DIR" ]]; then
        echo "Error: FlameGraph toolkit not found at $FLAMEGRAPH_DIR" >&2
        echo "       Clone it: git clone https://github.com/brendangregg/FlameGraph.git ~/FlameGraph" >&2
        echo "       Or pass a different path: --flamegraph-dir <path>" >&2
        exit 1
    fi
    if [[ ! -x "${FLAMEGRAPH_DIR}/stackcollapse-gdb.pl" ]]; then
        echo "Error: stackcollapse-gdb.pl not found in $FLAMEGRAPH_DIR" >&2
        echo "       Update your FlameGraph clone: git -C $FLAMEGRAPH_DIR pull" >&2
        exit 1
    fi
    # Warm up the sudo credential cache while we still have a foreground
    # terminal. The sampling loop runs in a background subshell where sudo
    # has no tty to prompt on, so it would block indefinitely without this.
    echo "    Authenticating sudo (required for gdb to attach to PID $PID) ..."
    sudo -v
}

# ---------------------------------------------------------------------------
# Stop-wait: block until the user presses ENTER, or — when --timeout is set —
# until TIMEOUT seconds elapse (ENTER still stops early). The watchdog feeds a
# newline to unblock this in either mode if the target process dies.
# ---------------------------------------------------------------------------
wait_for_stop() {
    if [[ -n "$TIMEOUT" ]]; then
        read -t "$TIMEOUT" -rp "    [recording...] auto-stop in ${TIMEOUT}s (ENTER to stop early) → " _ || true
    else
        read -rp "    [recording...] Press ENTER to stop → " _ || true
    fi
}

# ---------------------------------------------------------------------------
# Step 4 — record
# ---------------------------------------------------------------------------
echo "[4] Starting $PROFILER recording for PID $PID ..."
echo "    → Run your workload now (COPY, query, etc.)"
if [[ -n "$TIMEOUT" ]]; then
    echo "    → Recording will stop automatically after ${TIMEOUT}s (or press ENTER to stop early)."
else
    echo "    → Press ENTER when you want to stop recording."
fi
echo ""

case "$PROFILER" in

    perf)
        check_deps_perf

        sudo perf record \
            -F "$FREQUENCY" \
            -p "$PID" \
            -g --call-graph dwarf \
            -o "$PERF_DATA" &
        PROFILER_PID=$!

        # Watchdog: unblocks the read prompt if the target process dies
        (
            while sudo kill -0 "$PID" 2>/dev/null; do
                sleep 1
            done
            echo ""
            echo "    [watchdog] Process $PID terminated. Stopping recording."
            sudo kill -SIGINT "$PROFILER_PID" 2>/dev/null || true
            # Feed a newline to unblock the foreground read -rp
            echo "" > /dev/tty
        ) &
        WATCHDOG_PID=$!

        wait_for_stop

        kill "$WATCHDOG_PID" 2>/dev/null || true
        sudo kill -SIGINT "$PROFILER_PID" 2>/dev/null || true
        wait "$PROFILER_PID" 2>/dev/null || true
        ;;

    gdb)
        check_deps_gdb

        SLEEP_TIME=$(echo "scale=6; 1 / $FREQUENCY" | bc -l)
        STOP_FLAG=$(mktemp)
        rm -f "$STOP_FLAG"
        GDB_ERRORS="${OUT_DIR}/pg_gdb_errors.log"

        # Run the entire sampling loop under a single sudo bash invocation.
        # This way sudo authenticates once and the credential is held for the
        # lifetime of that shell — no keepalive needed, no per-call sudo
        # re-authentication, no dependency on timestamp_type configuration.
        sudo bash -c "
            while [[ ! -f '$STOP_FLAG' ]]; do
                if ! kill -0 '$PID' 2>/dev/null; then
                    echo '[profiler] Target process $PID terminated.' >&2
                    break
                fi
                gdb -ex 'set pagination 0'                     -ex 'thread apply all bt'                     -batch -p '$PID' 2>>'$GDB_ERRORS'
                sleep '$SLEEP_TIME'
            done
        " >> "$GDB_RAW" &
        PROFILER_PID=$!

        # ENTER listener reads from /dev/tty so gdb can't steal stdin
        ( read -r _ < /dev/tty; touch "$STOP_FLAG" ) &
        READER_PID=$!

        # Watchdog: unblocks the read prompt if the target process dies
        (
            while kill -0 "$PID" 2>/dev/null || sudo kill -0 "$PID" 2>/dev/null; do
                sleep 1
            done
            echo ""
            echo "    [watchdog] Process $PID terminated. Stopping recording."
            touch "$STOP_FLAG"
            # Feed a newline to unblock the foreground read -rp
            echo "" > /dev/tty
        ) &
        WATCHDOG_PID=$!

        wait_for_stop

        touch "$STOP_FLAG"
        wait "$PROFILER_PID"  2>/dev/null || true
        kill "$READER_PID"    2>/dev/null || true
        kill "$WATCHDOG_PID"  2>/dev/null || true
        rm -f "$STOP_FLAG"

        # Warn if any gdb errors were recorded
        if [[ -s "$GDB_ERRORS" ]]; then
            echo "    [warning] gdb produced errors during recording." >&2
            echo "              Check: $GDB_ERRORS" >&2
        fi
        ;;
esac

echo ""
echo "    Recording stopped."
echo ""

# ---------------------------------------------------------------------------
# Step 5a — extract & collapse stacks
# ---------------------------------------------------------------------------
echo "[5a] Collapsing stacks ..."

case "$PROFILER" in
    perf)
        echo "    → Extracting stacks from perf data ..."
        sudo perf script -i "$PERF_DATA" > "$PERF_SCRIPT"

        echo "    → Collapsing with stackcollapse-perf.pl ..."
        "${FLAMEGRAPH_DIR}/stackcollapse-perf.pl" "$PERF_SCRIPT" > "$FOLDED"
        ;;
    gdb)
        echo "    → Collapsing with stackcollapse-gdb.pl ..."
        "${FLAMEGRAPH_DIR}/stackcollapse-gdb.pl" "$GDB_RAW" > "$FOLDED"
        ;;
esac

echo "    Folded stacks saved to: $FOLDED"
echo ""

# ---------------------------------------------------------------------------
# Step 5b — render SVG
# ---------------------------------------------------------------------------
echo "[5b] Rendering flamegraph SVG ..."
"${FLAMEGRAPH_DIR}/flamegraph.pl" \
    --title "PostgreSQL PID $PID — $(date -d "@${TIMESTAMP:0:8}" +"%Y-%m-%d" 2>/dev/null || echo "$TIMESTAMP")" \
    --colors blue \
    --width 1600 \
    "$FOLDED" > "$SVG"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "============================================="
echo "  Done! Output files:"
case "$PROFILER" in
    perf)
        echo "    perf data  : $PERF_DATA"
        echo "    perf script: $PERF_SCRIPT"
        ;;
    gdb)
        echo "    gdb raw    : $GDB_RAW"
        echo "    gdb errors : $GDB_ERRORS"
        ;;
esac
echo "    folded     : $FOLDED"
echo "    flamegraph : $SVG"
echo "============================================="
echo ""
echo "  View locally : xdg-open $SVG"
echo "  Serve remote : python3 -m http.server 8080 --directory $OUT_DIR"
echo "  Copy locally : scp user@192.168.65.2:$SVG ~/pg_flamegraph.svg"
echo "============================================="
