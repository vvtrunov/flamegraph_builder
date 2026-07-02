"""Configuration: defaults and the immutable Config passed through the app."""

from dataclasses import dataclass

# Defaults mirror build_flamegraph.sh where they overlap.
DEFAULT_PROFILER = "perf"
DEFAULT_FREQUENCY = 99.0
DEFAULT_DURATION = 5.0          # layer-1 sampling seconds per process per iteration
DEFAULT_INTERVAL = 5.0          # sleep between cycles ("sleep for a while")
DEFAULT_FLAMEGRAPH_DIR = "/home/user/work/FlameGraph"

# Flamegraph rendering knobs (shared by per-process and grand-total SVGs).
FLAMEGRAPH_WIDTH = 1600
FLAMEGRAPH_COLORS = "blue"


@dataclass(frozen=True)
class Config:
    name_regex: str          # raw regex string; compiled in discovery
    out_base: str            # user-supplied base dir; run dir is created inside it
    profiler: str            # "perf" | "gdb"
    frequency: float         # Hz
    duration: float          # seconds per iteration
    interval: float          # seconds between cycles
    merge_all: bool          # also build one grand-total flamegraph
    flamegraph_dir: str      # FlameGraph toolkit clone
