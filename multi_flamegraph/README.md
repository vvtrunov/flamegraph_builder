# multi_flamegraph

Name-matched, multi-process, **2-layer** profiler that produces one flamegraph per
process (plus an optional grand-total) — built as a more capable sibling of
`../build_flamegraph.sh`.

## What it does

1. **Discovers by name.** You pass a regex (`--name`); every process whose *full
   command line* matches is profiled — not a single fixed PID.
2. **Re-scans every cycle.** Before each round it runs `ps` again, so processes that
   start *later* are picked up automatically and get their own output directory.
3. **Two-layer sampling.**
   - *Layer 1* — perf or gdb samples each matching process for `--duration` seconds.
   - *Layer 2* — the script repeats those iterations (sleeping `--interval` between
     them) and later merges all of a process's iterations into one aggregated profile.
4. **Keeps processes isolated.** Each PID gets `pid_<PID>/` with its per-iteration
   folded stacks and a snapshot of its command line at each iteration.
5. **Merges & renders.** Per process → `final.folded` + `final.svg`. With
   `--merge-all`, every process's `final.folded` is merged once more into a single
   `all/all.folded` + `all/all.svg`.

Runs until you press **ENTER** (or Ctrl-C); processes that exit keep their data.

## Why gdb for the combined view

The point of the gdb backend is a **single flamegraph combining on- and off-CPU
load**. `gdb thread apply all bt` captures the stack every tick *regardless of CPU
state*, in one uniform unit, so a blocking `read`/`write`/`epoll_wait` shows up on top
of the stack for I/O-bound workloads — on- and off-CPU on the same graph.

- `--profiler gdb` → combined on+off-CPU (wall-clock). **Primary use case.**
- `--profiler perf` → low-overhead **on-CPU only**.

(System-wide perf, eBPF `offcputime`, and Parca/Pyroscope were considered; none give
this clean single combined-load picture without whole-system overhead.)

## Usage

```bash
# On-CPU (perf) flamegraph for every postgres backend
python3 -m multi_flamegraph --name postgres --out ./prof

# Combined on+off-CPU view (gdb): I/O and lock waits on one flamegraph
python3 -m multi_flamegraph --name 'postgres.*COPY' --out ./prof --profiler gdb

# Aggregate every match into one grand-total flamegraph as well
python3 -m multi_flamegraph --name nginx --out ./prof --merge-all \
        --duration 5 --interval 3
```

### Options

| Flag | Default | Meaning |
|------|---------|---------|
| `--name`, `-n` | *required* | Regex matched against each process's full command line. |
| `--out`, `-o` | *required* | Base dir; a timestamped run dir is created inside it. |
| `--profiler` | `perf` | `perf` (on-CPU) or `gdb` (combined on+off-CPU). |
| `--duration`, `-d` | `5` | Seconds sampled per process per iteration. |
| `--interval`, `-i` | `5` | Seconds slept between cycles. |
| `--frequency`, `-f` | `99` | Hz: perf `-F`, or gdb sample interval = `1/frequency`. |
| `--merge-all` | off | Also build one grand-total flamegraph across all processes. |
| `--flamegraph-dir` | auto | FlameGraph toolkit clone. Auto-resolved (see below) if omitted. |

## Output layout

```
<out>/<YYYYMMDD_HHMMSS>/
  pid_<PID>/
    iter_0001.cmdline.txt   command line captured at that iteration's start
    iter_0001.folded        layer-1 folded stacks for that sample
    ...
    final.folded            layer-2 merge of all iterations
    final.svg               flamegraph of final.folded
  all/                      only with --merge-all
    all.folded              merge of every pid_*/final.folded
    all.svg
```

## Codebase map

| File | Responsibility |
|------|----------------|
| `__main__.py` | Entry point (`python3 -m multi_flamegraph`). |
| `cli.py` | Argument parsing, validation, wiring. |
| `config.py` | `Config` dataclass and default constants. |
| `deps.py` | Backend/toolkit checks; sudo warm-up + keepalive. |
| `discovery.py` | `scan_processes()` via `ps`; regex match on the full command line. |
| `folded.py` | **Reusable** folded I/O + `merge_folded()` (used at both merge levels). |
| `profilers.py` | `profile_once()` for the perf and gdb backends → folded. |
| `render.py` | `render_flamegraph()` wrapping `flamegraph.pl`. |
| `orchestrator.py` | Run loop: scan → parallel sample → repeat → finalize + merge-all. |

## Requirements

- Python 3 (standard library only).
- `perf` (for `--profiler perf`) or `gdb` (for `--profiler gdb`).
- A clone of [FlameGraph](https://github.com/brendangregg/FlameGraph). No absolute path
  is baked in; the toolkit is located in this order:
  `--flamegraph-dir` → `$FLAMEGRAPH_DIR` → `flamegraph.pl` on `$PATH` → `~/FlameGraph`.
  So on a new machine, either clone it to `~/FlameGraph`, export `FLAMEGRAPH_DIR`, or pass
  `--flamegraph-dir /path/to/FlameGraph`.
- **sudo** — both backends attach to other processes, so root is required. You are
  prompted once at startup; a keepalive holds the credential for long runs.

## Known limitations

- A process that both **starts and exits within a single `--interval` sleep** (never
  alive at a scan instant) is missed — keep `--interval` short to narrow the window.
- The gdb backend **ptrace-stops** its target briefly on each sample (some workload
  perturbation, coarse sample rate); it is the cost of the combined on+off-CPU view.
- Per-process directories are keyed by PID; a **reused PID** within one run maps to
  the same `pid_<PID>` directory.
