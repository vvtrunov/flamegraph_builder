"""Run loop: discover -> sample all matches in parallel -> repeat -> finalize.

Per-cycle it re-scans `ps`, so processes started later (t1 > t0) are picked up and
get their own per-PID directory. Runs until ENTER; a process that exits simply stops
matching (its data is preserved). On finish, each process's iterations are merged into
final.folded + final.svg, and --merge-all adds one grand-total flamegraph.
"""

import os
import threading
from datetime import datetime
from typing import Dict, List

from .config import Config
from . import discovery, folded, render
from .profilers import profile_once


class _Proc:
    """Per-process state: its output dir and how many iterations it has had."""

    def __init__(self, pid: int, directory: str):
        self.pid = pid
        self.dir = directory
        self.iterations = 0


class Orchestrator:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.pattern = discovery.compile_pattern(cfg.name_regex)
        self.stop = threading.Event()
        self.procs: Dict[int, _Proc] = {}
        # Timestamped run dir keeps separate runs from mixing.
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.run_dir = os.path.join(cfg.out_base, stamp)

    # -- lifecycle ---------------------------------------------------------

    def run(self) -> str:
        os.makedirs(self.run_dir, exist_ok=True)
        self._print_header()
        self._start_enter_listener()

        cycle = 0
        try:
            while not self.stop.is_set():
                cycle += 1
                self._run_cycle(cycle)
                # Interruptible sleep between cycles; ENTER wakes it immediately.
                self.stop.wait(self.cfg.interval)
        except KeyboardInterrupt:
            # Ctrl-C is a valid stop signal too: finalize what we have.
            self.stop.set()

        print("\n    Recording stopped. Finalizing ...")
        self._finalize()
        return self.run_dir

    # -- one cycle ---------------------------------------------------------

    def _run_cycle(self, cycle: int) -> None:
        matches = discovery.scan_processes(self.pattern)
        if not matches:
            print(f"  [cycle {cycle}] no processes match /{self.cfg.name_regex}/")
            return

        print(f"  [cycle {cycle}] sampling {len(matches)} process(es) "
              f"for {self.cfg.duration}s ...")

        threads: List[threading.Thread] = []
        for pid, cmdline in matches:
            proc = self._ensure_proc(pid)
            proc.iterations += 1
            self._write_cmdline(proc, cmdline)
            # One thread per PID: each supervises a perf/gdb subprocess (I/O-bound).
            t = threading.Thread(target=self._sample, args=(proc,))
            t.start()
            threads.append(t)
        for t in threads:
            t.join()

    def _sample(self, proc: _Proc) -> None:
        out_folded = os.path.join(proc.dir, f"iter_{proc.iterations:04d}.folded")
        try:
            profile_once(self.cfg, proc.pid, out_folded)
        except Exception as exc:  # keep other processes/cycles going
            print(f"    [pid {proc.pid}] sample error: {exc}")

    # -- per-process bookkeeping ------------------------------------------

    def _ensure_proc(self, pid: int) -> _Proc:
        proc = self.procs.get(pid)
        if proc is None:
            directory = os.path.join(self.run_dir, f"pid_{pid}")
            os.makedirs(directory, exist_ok=True)
            proc = _Proc(pid, directory)
            self.procs[pid] = proc
            print(f"    [pid {pid}] discovered -> {directory}")
        return proc

    def _write_cmdline(self, proc: _Proc, cmdline: str) -> None:
        # Snapshot the command line at the start of this iteration.
        path = os.path.join(proc.dir, f"iter_{proc.iterations:04d}.cmdline.txt")
        with open(path, "w") as fh:
            fh.write(cmdline + "\n")

    # -- finalize ----------------------------------------------------------

    def _finalize(self) -> None:
        finals: List[str] = []
        for pid, proc in sorted(self.procs.items()):
            iters = sorted(
                os.path.join(proc.dir, f)
                for f in os.listdir(proc.dir)
                if f.startswith("iter_") and f.endswith(".folded")
            )
            final_folded = os.path.join(proc.dir, "final.folded")
            total = folded.merge_folded(iters, final_folded)  # layer-2 aggregation
            if total == 0:
                print(f"    [pid {pid}] no samples collected; skipping flamegraph")
                continue
            svg = os.path.join(proc.dir, "final.svg")
            render.render_flamegraph(
                self.cfg, final_folded, svg,
                title=f"PID {pid} — {self.cfg.profiler} — {total} samples")
            finals.append(final_folded)
            print(f"    [pid {pid}] {proc.iterations} iters, {total} samples -> {svg}")

        if self.cfg.merge_all and finals:
            self._merge_all(finals)

    def _merge_all(self, finals: List[str]) -> None:
        all_dir = os.path.join(self.run_dir, "all")
        os.makedirs(all_dir, exist_ok=True)
        all_folded = os.path.join(all_dir, "all.folded")
        # SAME reusable merge, one level up: every process's final -> grand total.
        total = folded.merge_folded(finals, all_folded)
        svg = os.path.join(all_dir, "all.svg")
        render.render_flamegraph(
            self.cfg, all_folded, svg,
            title=f"ALL /{self.cfg.name_regex}/ — {self.cfg.profiler} — {total} samples")
        print(f"    [merge-all] {len(finals)} processes, {total} samples -> {svg}")

    # -- UI helpers --------------------------------------------------------

    def _start_enter_listener(self) -> None:
        # Read ENTER from the controlling tty so backend subprocesses can't steal it.
        def _listen() -> None:
            try:
                with open("/dev/tty") as tty:
                    tty.readline()
            except OSError:
                return
            self.stop.set()

        threading.Thread(target=_listen, daemon=True).start()

    def _print_header(self) -> None:
        print("=============================================")
        print("  multi-process flamegraph profiler")
        print("=============================================")
        print(f"  Name regex : /{self.cfg.name_regex}/")
        print(f"  Backend    : {self.cfg.profiler}"
              f"{'  (on+off-CPU wall-clock)' if self.cfg.profiler == 'gdb' else '  (on-CPU)'}")
        print(f"  Duration   : {self.cfg.duration}s per iteration")
        print(f"  Interval   : {self.cfg.interval}s between cycles")
        print(f"  Merge all  : {'yes' if self.cfg.merge_all else 'no'}")
        print(f"  Run dir    : {self.run_dir}")
        print("=============================================")
        print("  → Start/continue your workload now.")
        print("  → Press ENTER to stop profiling.")
        print("")
