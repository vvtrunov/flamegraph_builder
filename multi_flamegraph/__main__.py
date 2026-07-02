"""Entry point: `python3 -m multi_flamegraph`."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
