"""Render a folded-stack file into a flamegraph SVG via FlameGraph's flamegraph.pl."""

import os
import subprocess

from .config import Config, FLAMEGRAPH_COLORS, FLAMEGRAPH_WIDTH


def render_flamegraph(cfg: Config, folded_path: str, svg_path: str, title: str) -> bool:
    """Run flamegraph.pl on ``folded_path`` -> ``svg_path``. Returns success.

    Skips (returns False) when the folded file is empty, since flamegraph.pl on an
    empty input produces a useless SVG and just noise in the logs.
    """
    if not os.path.exists(folded_path) or os.path.getsize(folded_path) == 0:
        return False

    flamegraph_pl = os.path.join(cfg.flamegraph_dir, "flamegraph.pl")
    with open(svg_path, "w") as svg:
        proc = subprocess.run(
            [
                flamegraph_pl,
                "--title", title,
                "--colors", FLAMEGRAPH_COLORS,
                "--width", str(FLAMEGRAPH_WIDTH),
                folded_path,
            ],
            stdout=svg,
        )
    return proc.returncode == 0
