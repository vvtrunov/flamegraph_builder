"""Configuration: defaults and the immutable Config passed through the app."""

import os
import shutil
from dataclasses import dataclass

# Defaults mirror build_flamegraph.sh where they overlap.
DEFAULT_PROFILER = "perf"
DEFAULT_FREQUENCY = 99.0
DEFAULT_DURATION = 5.0          # layer-1 sampling seconds per process per iteration
DEFAULT_INTERVAL = 5.0          # sleep between cycles ("sleep for a while")

# No hardcoded absolute FlameGraph path — resolved portably at runtime (see below).
FLAMEGRAPH_ENV = "FLAMEGRAPH_DIR"
FLAMEGRAPH_FALLBACK = "~/FlameGraph"

# Flamegraph rendering knobs (shared by per-process and grand-total SVGs).
FLAMEGRAPH_WIDTH = 1600
FLAMEGRAPH_COLORS = "blue"


def resolve_flamegraph_dir(explicit: str = None) -> str:
    """Locate the FlameGraph toolkit without baking in an absolute path.

    Precedence: --flamegraph-dir > $FLAMEGRAPH_DIR > flamegraph.pl on $PATH > ~/FlameGraph.
    Always returns an absolute path (~ and env vars expanded).
    """
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    env = os.environ.get(FLAMEGRAPH_ENV)
    if env:
        return os.path.abspath(os.path.expanduser(env))
    on_path = shutil.which("flamegraph.pl")
    if on_path:
        return os.path.dirname(os.path.abspath(on_path))
    return os.path.abspath(os.path.expanduser(FLAMEGRAPH_FALLBACK))


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
