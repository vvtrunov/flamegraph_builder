"""Run loop: discover -> assign to groups -> sample in parallel -> repeat -> finalize.

Per-cycle it re-scans `ps`, so processes started later (t1 > t0) are picked up and get
their own per-PID directory. Each process is assigned to exactly one group (a PID matching
more than one group is a fatal GroupOverlapError). Runs until ENTER; a process that exits
simply stops matching (its data is preserved). On finish, each process's iterations are
merged into final.folded + final.svg, and per-group merge_all adds a group-level flamegraph.
Merging never crosses group boundaries.
"""

import os
import threading
from datetime import datetime
from typing import Dict, List, Tuple

from .config import Config, Group
from . import discovery, folded, render
from .discovery import GroupOverlapError
from .profilers import profile_once


class _Proc:
    """Per-process state: its output dir and how many iterations it has had."""

    def __init__(self, pid: int, directory: str):
        self.pid = pid
        self.dir = directory
        self.iterations = 0


class _GroupState:
    """A group plus its compiled pattern, output base dir, and discovered procs."""

    def __init__(self, group: Group, base_dir: str):
        self.group = group
        self.pattern = discovery.compile_pattern(group.regexp)
        self.base_dir = base_dir
        self.procs: Dict[int, _Proc] = {}


class Orchestrator:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.stop = threading.Event()
        # Timestamped run dir keeps separate runs from mixing.
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.run_dir = os.path.join(cfg.out_base, stamp)
        # An empty suffix (the --name case) keeps the flat layout: base = run dir.
        self.groups: List[_GroupState] = [
            _GroupState(g, self.run_dir if not g.suffix
                        else os.path.join(self.run_dir, g.suffix))
            for g in cfg.groups
        ]

    # -- lifecycle ---------------------------------------------------------

    def run(self) -> Tuple[str, bool]:
        """Run until ENTER/Ctrl-C (ok=True) or a group overlap (ok=False).

        Always finalizes whatever was collected before returning.
        """
        os.makedirs(self.run_dir, exist_ok=True)
        for gs in self.groups:
            os.makedirs(gs.base_dir, exist_ok=True)
        self._print_header()
        self._start_enter_listener()

        ok = True
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
        except GroupOverlapError as exc:
            # Ambiguous assignment is fatal: stop, but still keep collected data.
            print(f"\n    [FATAL] {exc}")
            self.stop.set()
            ok = False

        print("\n    Recording stopped. Finalizing ...")
        self._finalize()
        return self.run_dir, ok

    # -- one cycle ---------------------------------------------------------

    def _run_cycle(self, cycle: int) -> None:
        # Scan once, then assign each process to its single matching group.
        assigned: List[Tuple[_GroupState, _Proc]] = []
        for pid, cmdline in discovery.list_processes():
            gs = discovery.match_group(pid, cmdline, self.groups)  # may raise overlap
            if gs is None:
                continue
            proc = self._ensure_proc(gs, pid)
            proc.iterations += 1
            self._write_cmdline(proc, cmdline)
            assigned.append((gs, proc))

        if not assigned:
            print(f"  [cycle {cycle}] no matching processes")
            return

        print(f"  [cycle {cycle}] sampling {len(assigned)} process(es) "
              f"for {self.cfg.duration}s ...")
        # One thread per PID; each PID is in exactly one group, so no two threads
        # attach the same PID (which would break gdb's exclusive ptrace).
        threads = [threading.Thread(target=self._sample, args=(proc,))
                   for _, proc in assigned]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    def _sample(self, proc: _Proc) -> None:
        out_folded = os.path.join(proc.dir, f"iter_{proc.iterations:04d}.folded")
        try:
            profile_once(self.cfg, proc.pid, out_folded)
        except Exception as exc:  # keep other processes/cycles going
            print(f"    [pid {proc.pid}] sample error: {exc}")

    # -- per-process bookkeeping ------------------------------------------

    def _ensure_proc(self, gs: _GroupState, pid: int) -> _Proc:
        proc = gs.procs.get(pid)
        if proc is None:
            directory = os.path.join(gs.base_dir, f"pid_{pid}")
            os.makedirs(directory, exist_ok=True)
            proc = _Proc(pid, directory)
            gs.procs[pid] = proc
            label = gs.group.suffix or "(default)"
            print(f"    [pid {pid}] discovered in group '{label}' -> {directory}")
        return proc

    def _write_cmdline(self, proc: _Proc, cmdline: str) -> None:
        # Snapshot the command line at the start of this iteration.
        path = os.path.join(proc.dir, f"iter_{proc.iterations:04d}.cmdline.txt")
        with open(path, "w") as fh:
            fh.write(cmdline + "\n")

    # -- finalize ----------------------------------------------------------

    def _finalize(self) -> None:
        for gs in self.groups:
            label = gs.group.suffix or "(default)"
            finals: List[str] = []
            for pid, proc in sorted(gs.procs.items()):
                iters = sorted(
                    os.path.join(proc.dir, f)
                    for f in os.listdir(proc.dir)
                    if f.startswith("iter_") and f.endswith(".folded")
                )
                final_folded = os.path.join(proc.dir, "final.folded")
                total = folded.merge_folded(iters, final_folded)  # layer-2 aggregation
                if total == 0:
                    print(f"    [{label}/pid {pid}] no samples; skipping flamegraph")
                    continue
                svg = os.path.join(proc.dir, "final.svg")
                render.render_flamegraph(
                    self.cfg, final_folded, svg,
                    title=f"PID {pid} — {self.cfg.profiler} — {total} samples")
                finals.append(final_folded)
                print(f"    [{label}/pid {pid}] {proc.iterations} iters, "
                      f"{total} samples -> {svg}")

            # Per-group merge only; never across groups.
            if gs.group.merge_all and finals:
                self._merge_group(gs, finals)

    def _merge_group(self, gs: _GroupState, finals: List[str]) -> None:
        label = gs.group.suffix or "(default)"
        all_dir = os.path.join(gs.base_dir, "all")
        os.makedirs(all_dir, exist_ok=True)
        all_folded = os.path.join(all_dir, "all.folded")
        # SAME reusable merge, one level up: this group's finals -> group total.
        total = folded.merge_folded(finals, all_folded)
        svg = os.path.join(all_dir, "all.svg")
        render.render_flamegraph(
            self.cfg, all_folded, svg,
            title=f"GROUP {label} — {self.cfg.profiler} — {total} samples")
        print(f"    [{label}/merge-all] {len(finals)} processes, "
              f"{total} samples -> {svg}")

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
        print(f"  Backend    : {self.cfg.profiler}"
              f"{'  (on+off-CPU wall-clock)' if self.cfg.profiler == 'gdb' else '  (on-CPU)'}")
        print(f"  Duration   : {self.cfg.duration}s per iteration")
        print(f"  Interval   : {self.cfg.interval}s between cycles")
        print(f"  Groups     : {len(self.groups)}")
        for gs in self.groups:
            label = gs.group.suffix or "(default)"
            merge = "merge" if gs.group.merge_all else "no-merge"
            print(f"    - {label:<16} /{gs.group.regexp}/  [{merge}]")
        print(f"  Run dir    : {self.run_dir}")
        print("=============================================")
        print("  → Start/continue your workload now.")
        print("  → Press ENTER to stop profiling.")
        print("")
