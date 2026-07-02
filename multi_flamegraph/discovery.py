"""Process discovery: find PIDs whose full command line matches the name regex."""

import os
import re
import subprocess
from typing import Dict, List, Tuple

# `ps -eo pid=,args=` prints "  <pid> <full command line>" with no header. args= is
# the full command line, so the regex can match interpreters, paths, and flags —
# not just the short comm name (which may differ, per the design).
_PS_CMD = ["ps", "-eo", "pid=,args="]


def scan_processes(pattern: re.Pattern) -> List[Tuple[int, str]]:
    """Return [(pid, cmdline)] for live processes whose cmdline matches ``pattern``.

    Excludes our own PID so the profiler never profiles itself.
    """
    self_pid = os.getpid()
    out = subprocess.run(_PS_CMD, capture_output=True, text=True).stdout

    matches: List[Tuple[int, str]] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        pid_str, sep, cmdline = line.partition(" ")
        if not sep or not pid_str.isdigit():
            continue
        pid = int(pid_str)
        cmdline = cmdline.strip()
        if pid == self_pid:
            continue
        if pattern.search(cmdline):
            matches.append((pid, cmdline))
    return matches


def compile_pattern(regex: str) -> re.Pattern:
    """Compile the user's --name regex (raises re.error on invalid input)."""
    return re.compile(regex)
