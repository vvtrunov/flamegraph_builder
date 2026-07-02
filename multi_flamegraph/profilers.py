"""Backend sampling: profile a single PID for one iteration -> folded stacks.

Two backends, ported from build_flamegraph.sh:
  perf  On-CPU only. Fixed-duration `perf record` then perf script | stackcollapse.
  gdb   Wall-clock (on+off-CPU) via repeated `thread apply all bt` sampling. This is
        the backend that yields I/O/lock waits on a single flamegraph.
Both write a `.folded` file; raw artifacts live in a temp dir and are discarded.
"""

import os
import shutil
import subprocess
import tempfile

from .config import Config


def profile_once(cfg: Config, pid: int, out_folded: str) -> bool:
    """Sample ``pid`` for cfg.duration seconds; write folded stacks to out_folded.

    Returns True if a non-empty folded file was produced. Never raises on a target
    that dies mid-sample — that just yields whatever was captured so far.
    """
    if cfg.profiler == "perf":
        return _profile_perf(cfg, pid, out_folded)
    return _profile_gdb(cfg, pid, out_folded)


def _collapse(cfg: Config, script: str, raw_path: str, out_folded: str) -> bool:
    """Run a stackcollapse-*.pl over raw_path -> out_folded; report non-empty result."""
    collapse = os.path.join(cfg.flamegraph_dir, script)
    with open(out_folded, "w") as folded:
        subprocess.run([collapse, raw_path], stdout=folded)
    return os.path.exists(out_folded) and os.path.getsize(out_folded) > 0


def _profile_perf(cfg: Config, pid: int, out_folded: str) -> bool:
    # Managed manually (not TemporaryDirectory) because perf writes perf.data as
    # root; cleanup must also run as root to remove it.
    tmp = tempfile.mkdtemp(prefix="mflame_perf_")
    try:
        data = os.path.join(tmp, "perf.data")
        script_out = os.path.join(tmp, "perf.out")

        # `-- sleep <dur>` bounds the recording to a fixed duration; perf stops when
        # sleep exits (or earlier if the target dies).
        subprocess.run(
            ["sudo", "perf", "record",
             "-F", _int_str(cfg.frequency),
             "-p", str(pid),
             "-g", "--call-graph", "dwarf",
             "-o", data,
             "--", "sleep", _num_str(cfg.duration)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if not os.path.exists(data):
            return False

        # script_out is opened by us (user-owned); perf script writes into that fd.
        with open(script_out, "w") as so:
            subprocess.run(["sudo", "perf", "script", "-i", data],
                           stdout=so, stderr=subprocess.DEVNULL)
        return _collapse(cfg, "stackcollapse-perf.pl", script_out, out_folded)
    finally:
        # perf.data is root-owned, so remove the tree as root, then mop up as user.
        subprocess.run(["sudo", "rm", "-rf", tmp],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        shutil.rmtree(tmp, ignore_errors=True)


# Poor-man's-profiler loop (matches stackcollapse-gdb.pl's expected input). Args:
#   $1 = pid, $2 = duration seconds (int), $3 = sleep between samples.
_GDB_LOOP = (
    'deadline=$(( $(date +%s) + $2 ));'
    'while [ "$(date +%s)" -lt "$deadline" ]; do '
    '  kill -0 "$1" 2>/dev/null || break; '
    "  gdb -ex 'set pagination 0' -ex 'thread apply all bt' -batch -p \"$1\" 2>/dev/null; "
    '  sleep "$3"; '
    'done'
)


def _profile_gdb(cfg: Config, pid: int, out_folded: str) -> bool:
    sleep_between = 1.0 / cfg.frequency
    dur_seconds = max(1, int(round(cfg.duration)))  # gdb sampling is coarse; whole secs
    with tempfile.TemporaryDirectory(prefix="mflame_gdb_") as tmp:
        raw = os.path.join(tmp, "gdb_raw.out")
        # raw is opened by us (user-owned); the root loop writes into that fd, so no
        # root-owned files are left behind — plain TemporaryDirectory cleanup works.
        with open(raw, "w") as raw_fh:
            subprocess.run(
                ["sudo", "bash", "-c", _GDB_LOOP, "bash",
                 str(pid), str(dur_seconds), _num_str(sleep_between)],
                stdout=raw_fh, stderr=subprocess.DEVNULL,
            )
        return _collapse(cfg, "stackcollapse-gdb.pl", raw, out_folded)


def _int_str(value: float) -> str:
    """perf -F wants an integer Hz."""
    return str(int(round(value)))


def _num_str(value: float) -> str:
    """Render a number without a trailing .0 (e.g. 5.0 -> '5', 0.0101 -> '0.0101')."""
    if value == int(value):
        return str(int(value))
    return repr(value)
