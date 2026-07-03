"""Process discovery: list live processes and assign each to its matching group."""

import os
import re
import subprocess
from typing import List, Optional, Sequence, Tuple

# `ps -eo pid=,args=` prints "  <pid> <full command line>" with no header. args= is
# the full command line, so the regex can match interpreters, paths, and flags —
# not just the short comm name (which may differ, per the design).
_PS_CMD = ["ps", "-eo", "pid=,args="]


class GroupOverlapError(Exception):
    """A single PID matched more than one group's regexp (ambiguous assignment)."""

    def __init__(self, pid: int, cmdline: str, suffixes: Sequence[str]):
        self.pid = pid
        self.cmdline = cmdline
        self.suffixes = list(suffixes)
        shown = ", ".join(repr(s) for s in self.suffixes)
        super().__init__(
            f"PID {pid} matches multiple groups [{shown}]; regexps must be disjoint.\n"
            f"    cmdline: {cmdline}")


def list_processes() -> List[Tuple[int, str]]:
    """Return [(pid, cmdline)] for all live processes, excluding our own PID."""
    self_pid = os.getpid()
    out = subprocess.run(_PS_CMD, capture_output=True, text=True).stdout

    procs: List[Tuple[int, str]] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        pid_str, sep, cmdline = line.partition(" ")
        if not sep or not pid_str.isdigit():
            continue
        pid = int(pid_str)
        if pid == self_pid:
            continue
        procs.append((pid, cmdline.strip()))
    return procs


def match_group(pid: int, cmdline: str, group_states) -> Optional[object]:
    """Return the single group state whose pattern matches, or None if none match.

    Raises GroupOverlapError if more than one group matches (per the overlap policy).
    ``group_states`` items must expose ``.pattern`` (compiled) and ``.group.suffix``.
    """
    hits = [gs for gs in group_states if gs.pattern.search(cmdline)]
    if len(hits) > 1:
        raise GroupOverlapError(pid, cmdline, [gs.group.suffix for gs in hits])
    return hits[0] if hits else None


def compile_pattern(regex: str) -> "re.Pattern":
    """Compile a group's regex (raises re.error on invalid input)."""
    return re.compile(regex)
