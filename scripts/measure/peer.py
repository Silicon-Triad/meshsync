#!/usr/bin/env python3
"""Run one meshsync peer with the per-message metrics CSV switched on (#20, uses #17).

Usage: scripts/measure/peer.py CSV_PATH -- <meshsync run arguments>

Writes metrics.CSV_HEADER to CSV_PATH, attaches a FileHandler to metrics.CSV_LOGGER (the way
metrics/logger.py documents), then runs `meshsync run` in this process. Stop it with SIGINT
(Ctrl-C) so the node shuts down cleanly. Owner: Huzaifa Hanif (CS-24039).
"""

import logging
import sys

from meshsync import __main__ as cli
from meshsync.metrics import logger as metrics


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] != "--":
        print("usage: peer.py CSV_PATH -- <meshsync run arguments>", file=sys.stderr)
        return 2
    path = argv[0]
    with open(path, "w", encoding="utf-8") as f:
        f.write(metrics.CSV_HEADER + "\n")
    handler = logging.FileHandler(path, encoding="utf-8")  # appends after the header
    handler.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger(metrics.CSV_LOGGER).addHandler(handler)
    return cli.main(["run", *argv[2:]])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
