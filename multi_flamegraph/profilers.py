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
    errlog = _sibling(out_folded, ".perf_errors.log")
    # Managed manually (not TemporaryDirectory) because perf writes perf.data as
    # root; cleanup must also run as root to remove it.
    tmp = tempfile.mkdtemp(prefix="mflame_perf_")
    try:
        data = os.path.join(tmp, "perf.data")
        script_out = os.path.join(tmp, "perf.out")

        # Capture perf's stderr so an empty result is diagnosable (attach denied,
        # "no samples", etc.) instead of a silent empty file.
        with open(errlog, "w") as err:
            # `-- sleep <dur>` bounds the recording to a fixed duration; perf stops
            # when sleep exits (or earlier if the target dies).
            subprocess.run(
                ["sudo", "perf", "record",
                 "-F", _int_str(cfg.frequency),
                 "-p", str(pid),
                 "-g", "--call-graph", "dwarf",
                 "-o", data,
                 "--", "sleep", _num_str(cfg.duration)],
                stdout=subprocess.DEVNULL, stderr=err,
            )
            if not os.path.exists(data):
                return _keep_on_failure(errlog, False)

            # script_out is opened by us (user-owned); perf script writes into it.
            with open(script_out, "w") as so:
                subprocess.run(["sudo", "perf", "script", "-i", data],
                               stdout=so, stderr=err)
            ok = _collapse(cfg, "stackcollapse-perf.pl", script_out, out_folded)
        return _keep_on_failure(errlog, ok)
    finally:
        # perf.data is root-owned, so remove the tree as root, then mop up as user.
        subprocess.run(["sudo", "rm", "-rf", tmp],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        shutil.rmtree(tmp, ignore_errors=True)


# Poor-man's-profiler loop (matches stackcollapse-gdb.pl's expected input). Args:
#   $1 = pid, $2 = duration seconds (int), $3 = sleep between samples.
# gdb stderr is left un-redirected so the caller can capture attach failures.
_GDB_LOOP = (
    'deadline=$(( $(date +%s) + $2 ));'
    'while [ "$(date +%s)" -lt "$deadline" ]; do '
    '  kill -0 "$1" 2>/dev/null || break; '
    "  gdb -ex 'set pagination 0' -ex 'thread apply all bt' -batch -p \"$1\"; "
    '  sleep "$3"; '
    'done'
)


def _profile_gdb(cfg: Config, pid: int, out_folded: str) -> bool:
    sleep_between = 1.0 / cfg.frequency
    dur_seconds = max(1, int(round(cfg.duration)))  # gdb sampling is coarse; whole secs
    raw = _sibling(out_folded, ".gdb_raw.txt")
    errlog = _sibling(out_folded, ".gdb_errors.log")
    # raw/errlog are opened by us (user-owned); the root loop writes into those fds,
    # so no root-owned files are left behind.
    with open(raw, "w") as raw_fh, open(errlog, "w") as err_fh:
        subprocess.run(
            ["sudo", "bash", "-c", _GDB_LOOP, "bash",
             str(pid), str(dur_seconds), _num_str(sleep_between)],
            stdout=raw_fh, stderr=err_fh,
        )
    ok = _collapse(cfg, "stackcollapse-gdb.pl", raw, out_folded)
    if ok:
        _quiet_remove(raw)  # success: keep only the folded to bound disk
    # else: keep raw next to the empty folded so the failure can be inspected.
    return _keep_on_failure(errlog, ok)


def _sibling(out_folded: str, suffix: str) -> str:
    """Path next to out_folded: iter_0003.folded -> iter_0003<suffix>."""
    base = out_folded[:-len(".folded")] if out_folded.endswith(".folded") else out_folded
    return base + suffix


def _keep_on_failure(errlog: str, ok: bool) -> bool:
    """Drop the error log on success (or if empty); keep it when folded came out empty."""
    if ok or os.path.getsize(errlog) == 0:
        _quiet_remove(errlog)
    return ok


def _quiet_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _int_str(value: float) -> str:
    """perf -F wants an integer Hz."""
    return str(int(round(value)))


def _num_str(value: float) -> str:
    """Render a number without a trailing .0 (e.g. 5.0 -> '5', 0.0101 -> '0.0101')."""
    if value == int(value):
        return str(int(value))
    return repr(value)
