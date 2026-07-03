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

# Multiple isolated groups via a JSON config (per-group subdir + merge policy)
python3 -m multi_flamegraph --config groups.json --out ./prof --profiler gdb
```

## Stopping the profiler

The tool profiles until you stop it. How to stop:

| Action | Effect |
|--------|--------|
| **ENTER** (interactive only) | Graceful stop → merge + render flamegraphs. |
| **`SIGTERM`** (`kill <pid>`) | Same graceful stop — use this for scripted/background runs. |
| **Ctrl-C once** / 1st `SIGINT` | Graceful stop. |
| **Ctrl-C twice** / 2nd `SIGINT` | Abort now — kills in-flight `perf`/`gdb` children, **no** flamegraphs. |
| **`SIGKILL`** (`kill -9`) | OS-forced instant kill; you must `sudo pkill perf`/`gdb` yourself. |

A graceful stop lets the current sampling iteration finish (up to one `--duration`) before
finalizing. When not attached to a terminal (e.g. under the wrapper below), the ENTER
listener is skipped and you stop via `SIGTERM`.

### Profile only while a workload runs (`profile_during_workload.sh`)

A checkpointer/daemon never exits, so "profile only during the workload" needs an automatic
stop. The wrapper script (in the repo root) runs the profiler, watches `ps` for the workload's
loader process, and sends the graceful stop when it disappears:

```bash
# defaults target the stroppy loader + the multi_flamegraph command below
./profile_during_workload.sh

# or customize the workload regexp and the profiler command
./profile_during_workload.sh --pattern 'stroppy run' --poll 2 -- \
    python3 -m multi_flamegraph --out ./prof --profiler gdb --merge-all --config ../cfg.json
```

It authenticates sudo once up front, launches the profiler in the background, waits for a
process matching `--pattern` (a `pgrep -f` regexp) to appear, then stops the profiler the
moment no such process remains. Ctrl-C stops early (once = graceful, twice = abort).

### Options

| Flag | Default | Meaning |
|------|---------|---------|
| `--name`, `-n` | *one of* | Regex matched against each process's full command line (single implicit group). |
| `--config` | *one of* | JSON file defining groups. **Mutually exclusive** with `--name`; exactly one required. |
| `--out`, `-o` | *required* | Base dir; a timestamped run dir is created inside it. |
| `--profiler` | `perf` | `perf` (on-CPU) or `gdb` (combined on+off-CPU). |
| `--duration`, `-d` | `5` | Seconds sampled per process per iteration. |
| `--interval`, `-i` | `5` | Seconds slept between cycles. |
| `--frequency`, `-f` | `99` | Hz: perf `-F`, or gdb sample interval = `1/frequency`. |
| `--merge-all` | off | Merge each group's finals into a per-group flamegraph. With `--config`, this is the *default* for groups that omit `merge_all`. Never merges across groups. |
| `--flamegraph-dir` | auto | FlameGraph toolkit clone. Auto-resolved (see below) if omitted. |

## Groups (`--config`)

Profile many processes at once while keeping results **logically separated**. The config is
a JSON **list** of groups; each has its own regexp, output subdirectory, and merge policy:

```json
[
  { "regexp": "postgres.*COPY", "suffix": "copy_backends", "merge_all": true },
  { "regexp": "nginx: worker",  "suffix": "web" }
]
```

- `regexp` (required) — matched against the full command line.
- `suffix` (required) — the group's output subdirectory; a single path component, unique.
- `merge_all` (optional bool) — per-group merge; defaults to the `--merge-all` flag.

Merging happens **within** a group only, never across groups. A process whose command line
matches **more than one group is a fatal error**: the run stops (already-collected data is
still finalized) and exits non-zero — keep your group regexps disjoint.

## Output layout

With `--config` (one subdirectory per group):
```
<out>/<YYYYMMDD_HHMMSS>/
  <suffix>/                 # e.g. copy_backends
    pid_<PID>/
      iter_0001.cmdline.txt command line captured at that iteration's start
      iter_0001.folded      layer-1 folded stacks for that sample
      ...
      final.folded          layer-2 merge of all iterations
      final.svg             flamegraph of final.folded
    all/                    only if this group's merge_all is true
      all.folded            merge of this group's pid_*/final.folded
      all.svg
```

With `--name` (single implicit group — flat, unchanged):
```
<out>/<YYYYMMDD_HHMMSS>/
  pid_<PID>/ ...
  all/ ...                  only with --merge-all
```

If an iteration produces **no stacks**, the folded file is empty and the backend's
diagnostics are kept next to it for inspection: `iter_NNNN.gdb_raw.txt` +
`iter_NNNN.gdb_errors.log` (gdb) or `iter_NNNN.perf_errors.log` (perf). On success
these are removed to keep disk bounded. An empty result under **perf** is usually
just an idle (off-CPU) process — see below.

## Codebase map

| File | Responsibility |
|------|----------------|
| `__main__.py` | Entry point (`python3 -m multi_flamegraph`). |
| `cli.py` | Argument parsing, validation, wiring. |
| `config.py` | `Config`/`Group` dataclasses, defaults, and `load_groups()` (config JSON). |
| `deps.py` | Backend/toolkit checks; sudo warm-up + keepalive. |
| `discovery.py` | `list_processes()` via `ps`; `match_group()` + `GroupOverlapError`. |
| `folded.py` | **Reusable** folded I/O + `merge_folded()` (used at both merge levels). |
| `profilers.py` | `profile_once()` for the perf and gdb backends → folded. |
| `render.py` | `render_flamegraph()` wrapping `flamegraph.pl`. |
| `orchestrator.py` | Run loop: scan → assign to groups → parallel sample → finalize per group. |

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
