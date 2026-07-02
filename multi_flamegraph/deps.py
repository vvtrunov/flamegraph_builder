"""Dependency checks and sudo credential handling.

Both backends attach to processes owned by other users, so they need root. We warm
the sudo credential cache once up front (while a tty is available) and keep it alive
in a daemon thread, since the parallel sampling threads have no tty to prompt on.
"""

import os
import shutil
import subprocess
import threading
import time

from .config import Config

_SUDO_KEEPALIVE_SECONDS = 60


class DependencyError(Exception):
    """Raised when a required binary or FlameGraph script is missing."""


def _require_binary(name: str, hint: str) -> None:
    if shutil.which(name) is None:
        raise DependencyError(f"'{name}' not found. {hint}")


def _require_flamegraph_script(cfg: Config, script: str) -> None:
    path = os.path.join(cfg.flamegraph_dir, script)
    if not (os.path.isfile(path) and os.access(path, os.X_OK)):
        raise DependencyError(
            f"{script} not found (or not executable) in {cfg.flamegraph_dir}. "
            f"Clone/update FlameGraph: git clone "
            f"https://github.com/brendangregg/FlameGraph.git, or pass --flamegraph-dir."
        )


def check_dependencies(cfg: Config) -> None:
    """Verify the selected backend and FlameGraph toolkit are available."""
    if not os.path.isdir(cfg.flamegraph_dir):
        raise DependencyError(f"FlameGraph toolkit not found at {cfg.flamegraph_dir}")
    _require_flamegraph_script(cfg, "flamegraph.pl")

    if cfg.profiler == "perf":
        _require_binary("perf", "Install linux-tools or use --profiler gdb.")
        _require_flamegraph_script(cfg, "stackcollapse-perf.pl")
    else:  # gdb
        _require_binary("gdb", "Install gdb or use --profiler perf.")
        _require_flamegraph_script(cfg, "stackcollapse-gdb.pl")


def warm_up_sudo() -> None:
    """Prompt for sudo once, up front, and start a background keepalive."""
    print("    Authenticating sudo (required to attach to target processes) ...")
    subprocess.run(["sudo", "-v"], check=True)

    def _keepalive() -> None:
        # -n: never prompt; refresh the timestamp until the process exits.
        while True:
            time.sleep(_SUDO_KEEPALIVE_SECONDS)
            subprocess.run(["sudo", "-n", "-v"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    threading.Thread(target=_keepalive, daemon=True).start()
