"""Reusable folded-stack I/O and merging.

A "folded" line is the canonical FlameGraph input:  ``frame;frame;...;frame <count>``
where the count is the final whitespace-separated token. Merging = summing the
counts of identical stacks. This single merge is used at BOTH aggregation levels
(per-iteration -> per-process final, and per-process finals -> grand total), so the
folded format is preserved by construction.
"""

from collections import OrderedDict
from typing import Iterable


def parse_line(line: str):
    """Split a folded line into (stack, count). Return None for blank/malformed lines."""
    line = line.rstrip("\n")
    if not line.strip():
        return None
    # Count is the last token; the stack may itself contain spaces, so rsplit once.
    stack, sep, count = line.rpartition(" ")
    if not sep or not count.isdigit():
        return None
    return stack, int(count)


def merge_folded(input_paths: Iterable[str], output_path: str) -> int:
    """Sum counts per identical stack across all inputs; write sorted folded output.

    Returns the total sample count written. Missing inputs are skipped so a process
    that produced no samples in some iterations still merges cleanly.
    """
    totals: "OrderedDict[str, int]" = OrderedDict()
    for path in input_paths:
        try:
            with open(path, "r") as fh:
                for line in fh:
                    parsed = parse_line(line)
                    if parsed is None:
                        continue
                    stack, count = parsed
                    totals[stack] = totals.get(stack, 0) + count
        except FileNotFoundError:
            continue

    grand_total = 0
    with open(output_path, "w") as out:
        # Sort for stable, diffable output; order is irrelevant to flamegraph.pl.
        for stack in sorted(totals):
            count = totals[stack]
            grand_total += count
            out.write(f"{stack} {count}\n")
    return grand_total


def total_samples(path: str) -> int:
    """Sum of all counts in a folded file (0 if absent). Used for summaries/tests."""
    total = 0
    try:
        with open(path, "r") as fh:
            for line in fh:
                parsed = parse_line(line)
                if parsed is not None:
                    total += parsed[1]
    except FileNotFoundError:
        return 0
    return total
