# FROZEN INTERFACE (AGENTS.md §6): changing or adding to the public interface in this file
# needs an issue and approval from all three members first (R-6.2).
"""Per-message metrics: CSV + human-readable log (PROJECT_CONTEXT.md §8.3, §11, R6).

Owner: Huzaifa Hanif (CS-24039). Issue #17.
Contract (§8.3):
    metrics.record(direction, peer, type, size, t)

Every call produces two log records, both through the standard `logging` module so no new
public API (R-6.2) or invented file path is needed:

* `meshsync.metrics.logger` (DEBUG): human line, e.g. `send peer=bob type=PING size=20 t=...`.
  It propagates like any module log, so `--log-level DEBUG` shows it.
* `meshsync.metrics.csv` (INFO): one CSV row `t,direction,peer,type,size`. It does NOT
  propagate; nothing is written until a measurement script attaches a handler, e.g.
      h = logging.FileHandler("run.csv"); h.setFormatter(logging.Formatter("%(message)s"))
      logging.getLogger(CSV_LOGGER).addHandler(h)
  and writes CSV_HEADER first.

Called from net/connection.py (the only place that knows the peer name), not from framing:
send_msg/recv_msg take no peer argument and are frozen. `size` is bytes on the wire
(HEADER_SIZE + body), the "bytes on wire" metric of §11.
"""

import csv
import io
import logging

log = logging.getLogger(__name__)

CSV_LOGGER = "meshsync.metrics.csv"
CSV_HEADER = "t,direction,peer,type,size"
DIRECTIONS = ("send", "recv")

_csv_log = logging.getLogger(CSV_LOGGER)
_csv_log.propagate = False
_csv_log.setLevel(logging.INFO)


def _csv_row(*fields: object) -> str:
    buf = io.StringIO()
    csv.writer(buf, lineterminator="").writerow(fields)  # quotes peer names with commas
    return buf.getvalue()


def record(direction: str, peer: str, type: str, size: int, t: float) -> None:  # noqa: A002
    """Record one message. direction is "send" or "recv"; t is time.time() seconds."""
    if direction not in DIRECTIONS:
        raise ValueError(f"direction must be one of {DIRECTIONS}, got {direction!r}")
    if size < 0:
        raise ValueError(f"size must be >= 0, got {size}")
    if _csv_log.hasHandlers():
        _csv_log.info(_csv_row(f"{t:.6f}", direction, peer, type, size))
    log.debug("%s peer=%s type=%s size=%d t=%.6f", direction, peer, type, size, t)
