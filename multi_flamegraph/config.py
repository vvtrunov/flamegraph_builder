"""Configuration: defaults and the immutable Config passed through the app."""

import json
import os
import re
import shutil
from dataclasses import dataclass
from typing import List, Tuple

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
class Group:
    """One logically-isolated set of processes with its own subdir and merge policy."""
    regexp: str              # raw regex; compiled in the orchestrator
    suffix: str              # output subdir name; "" = the run dir itself (flat layout)
    merge_all: bool          # merge this group's finals into one flamegraph


@dataclass(frozen=True)
class Config:
    groups: Tuple[Group, ...]  # one implicit group (--name) or many (--config)
    out_base: str            # user-supplied base dir; run dir is created inside it
    profiler: str            # "perf" | "gdb"
    frequency: float         # Hz
    duration: float          # seconds per iteration
    interval: float          # seconds between cycles
    flamegraph_dir: str      # FlameGraph toolkit clone


def load_groups(path: str, default_merge_all: bool) -> List[Group]:
    """Parse + validate a --config JSON file into a list of Group.

    Raises ValueError with a precise message on any structural or value problem.
    """
    try:
        with open(path) as fh:
            data = json.load(fh)
    except FileNotFoundError:
        raise ValueError(f"--config file not found: {path}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"--config is not valid JSON: {exc}")

    if not isinstance(data, list) or not data:
        raise ValueError("--config must be a non-empty JSON list of group objects.")

    groups: List[Group] = []
    seen_suffixes = set()
    for i, item in enumerate(data):
        where = f"group #{i + 1}"
        if not isinstance(item, dict):
            raise ValueError(f"{where}: each config entry must be an object.")

        regexp = item.get("regexp")
        if not isinstance(regexp, str) or not regexp:
            raise ValueError(f"{where}: 'regexp' must be a non-empty string.")
        try:
            re.compile(regexp)
        except re.error as exc:
            raise ValueError(f"{where}: 'regexp' is not a valid regex: {exc}")

        suffix = item.get("suffix")
        if not isinstance(suffix, str) or not suffix:
            raise ValueError(f"{where}: 'suffix' must be a non-empty string.")
        # Keep suffix a single, safe subdirectory name.
        if "/" in suffix or os.sep in suffix or suffix in (".", ".."):
            raise ValueError(f"{where}: 'suffix' must be a single path component "
                             f"(no '/', not '.'/'..'): got '{suffix}'.")
        if suffix in seen_suffixes:
            raise ValueError(f"{where}: duplicate 'suffix' '{suffix}'.")
        seen_suffixes.add(suffix)

        merge_all = item.get("merge_all", default_merge_all)
        if not isinstance(merge_all, bool):
            raise ValueError(f"{where}: 'merge_all' must be true or false.")

        groups.append(Group(regexp=regexp, suffix=suffix, merge_all=merge_all))
    return groups
