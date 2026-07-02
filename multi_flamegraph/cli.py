"""Command-line interface: parse + validate args, then run the orchestrator."""

import argparse
import os
import re
import subprocess
import sys

from . import config
from .config import Config
from .deps import check_dependencies, warm_up_sudo, DependencyError


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="multi_flamegraph",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Name-matched, multi-process, 2-layer wall-clock profiler.",
        epilog=(
            "EXAMPLES\n"
            "  # On-CPU (perf) flamegraph for every postgres backend\n"
            "  python3 -m multi_flamegraph --name postgres --out ./prof\n\n"
            "  # Combined on+off-CPU view (gdb): I/O and lock waits on one flamegraph\n"
            "  python3 -m multi_flamegraph --name 'postgres.*COPY' --out ./prof "
            "--profiler gdb\n\n"
            "  # Aggregate all matches into one grand-total flamegraph too\n"
            "  python3 -m multi_flamegraph --name nginx --out ./prof --merge-all\n\n"
            "NOTE: per-process dirs are keyed by PID; a reused PID maps to the same dir."
        ),
    )
    p.add_argument("--name", "-n", required=True,
                   help="Regex matched against each process's full command line.")
    p.add_argument("--out", "-o", required=True,
                   help="Base output dir; a timestamped run dir is created inside it.")
    p.add_argument("--profiler", choices=["perf", "gdb"],
                   default=config.DEFAULT_PROFILER,
                   help="perf = on-CPU (default); gdb = combined on+off-CPU wall-clock.")
    p.add_argument("--duration", "-d", type=float, default=config.DEFAULT_DURATION,
                   help="Seconds of sampling per process per iteration (default: 5).")
    p.add_argument("--interval", "-i", type=float, default=config.DEFAULT_INTERVAL,
                   help="Seconds to sleep between cycles (default: 5).")
    p.add_argument("--frequency", "-f", type=float, default=config.DEFAULT_FREQUENCY,
                   help="Hz: perf -F, or gdb sample interval = 1/frequency (default: 99).")
    p.add_argument("--merge-all", action="store_true",
                   help="Also merge every process's final into one grand-total flamegraph.")
    p.add_argument("--flamegraph-dir", default=None,
                   help=("FlameGraph toolkit clone. If omitted, resolved from "
                         "$FLAMEGRAPH_DIR, then flamegraph.pl on $PATH, then ~/FlameGraph."))
    return p


def _positive(name: str, value: float) -> None:
    if value <= 0:
        raise ValueError(f"--{name} must be a positive number (got: {value}).")


def parse_args(argv) -> Config:
    args = build_parser().parse_args(argv)

    # Validate numbers and the regex up front so failures are immediate and clear.
    _positive("duration", args.duration)
    _positive("interval", args.interval)
    _positive("frequency", args.frequency)
    try:
        re.compile(args.name)
    except re.error as exc:
        raise ValueError(f"--name is not a valid regex: {exc}")

    return Config(
        name_regex=args.name,
        # expanduser so a passed "~/prof" works even when the shell didn't expand it.
        out_base=os.path.abspath(os.path.expanduser(args.out)),
        profiler=args.profiler,
        frequency=args.frequency,
        duration=args.duration,
        interval=args.interval,
        merge_all=args.merge_all,
        flamegraph_dir=config.resolve_flamegraph_dir(args.flamegraph_dir),
    )


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        cfg = parse_args(argv)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    try:
        check_dependencies(cfg)
    except DependencyError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    # Imported here so --help / validation work without any profiling deps present.
    from .orchestrator import Orchestrator

    try:
        warm_up_sudo()
    except subprocess.CalledProcessError:
        print("Error: sudo authentication failed; root is required to attach.",
              file=sys.stderr)
        return 1

    run_dir = Orchestrator(cfg).run()

    print("\n=============================================")
    print(f"  Done. Output in: {run_dir}")
    print(f"  View an SVG   : xdg-open {run_dir}/pid_<PID>/final.svg")
    print(f"  Serve remote  : python3 -m http.server 8080 --directory {run_dir}")
    print("=============================================")
    return 0
