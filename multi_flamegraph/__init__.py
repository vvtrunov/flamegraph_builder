"""multi_flamegraph — name-matched, multi-process, 2-layer wall-clock profiler.

Discovers processes by name regex, samples each one repeatedly (perf or gdb),
keeps per-process output isolated, then merges into per-process and optional
grand-total flamegraphs. See README.md for the high-level approach.
"""

__version__ = "0.1.0"
