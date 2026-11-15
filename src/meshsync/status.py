"""Status file and transfer statistics for `meshsync status` / `meshsync stats` (§14, §8.3).

Owner: Muhammad Ahmed Qazi (CS-24045), CLI area. Issue #64 (design decided there: option A).

The running node writes <sync_dir>/.meshsync/status.json (META_DIR) atomically: on start, on
peer up/down, on local and remote changes, and every SCAN_INTERVAL so the file's age shows the
node is alive. `meshsync status` reads it; a file older than STATUS_STALE_AFTER means the node is
not running. No socket and no wire message is involved (R-6.2).

`meshsync stats` summarises the per-message metrics CSV (#72; columns CSV_HEADER from
metrics/logger.py).
"""

import csv
import json
import logging
import os
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from meshsync.metrics.logger import CSV_HEADER, DIRECTIONS
from meshsync.protocol.constants import META_DIR, SCAN_INTERVAL

log = logging.getLogger(__name__)

STATUS_FILE = "status.json"
# #64: a CLI display threshold, not a protocol value, so it is not in constants.py (R-6.2).
# The node refreshes every SCAN_INTERVAL; 5x tolerates four refreshes delayed by a busy node
# (hashing or assembling a large file) before `status` reports "not running".
STATUS_STALE_AFTER = 5 * SCAN_INTERVAL


def status_path(sync_dir: Path) -> Path:
    return Path(sync_dir) / META_DIR / STATUS_FILE


# ---------------------------------------------------------------- writer (node side)


class StatusWriter:
    """Writes status.json. `info` holds the fixed fields (name, peer_id, mode, ports, ...);
    `snapshot()` returns the live ones (index_rev, entries, tombstones, peers).

    Never raises: a failed write is logged as WARNING and the node carries on (R-4.6). Each
    write uses its own temp file and fsyncs it without a lock (R-4.7); only the stopped check
    and os.replace run under _replace_lock, together with stop() setting the flag, so an
    update already in progress can never overwrite the stopped file (PR #86 review).
    """

    def __init__(self, sync_dir: Path, info: dict[str, Any]) -> None:
        self.path = status_path(sync_dir)
        self.info = dict(info, pid=os.getpid(), started_at=time.time())
        self._snapshot: Callable[[], dict[str, Any]] | None = None
        self._stop = threading.Event()
        self._replace_lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self, snapshot: Callable[[], dict[str, Any]]) -> None:
        self._snapshot = snapshot
        self.update()
        self._thread = threading.Thread(target=self._refresh_loop, name="status", daemon=True)
        self._thread.start()

    def update(self) -> None:
        if self._stop.is_set() or self._snapshot is None:
            return
        try:
            self._write({**self.info, **self._snapshot(), "stopped": False})
        except Exception:  # noqa: BLE001 - the status file must never stop the node (#64)
            log.warning("could not write %s", self.path, exc_info=True)

    def stop(self) -> None:
        """Mark the file stopped (kept, so `status` can show the last known state)."""
        with self._replace_lock:
            if self._stop.is_set():
                return
            self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=SCAN_INTERVAL)
        try:
            self._write({**self.info, **self._safe_snapshot(), "peers": [], "stopped": True})
        except Exception:  # noqa: BLE001
            log.warning("could not write %s", self.path, exc_info=True)

    def _safe_snapshot(self) -> dict[str, Any]:
        try:
            return self._snapshot() if self._snapshot else {}
        except Exception:  # noqa: BLE001
            log.warning("status snapshot failed", exc_info=True)
            return {}

    def _refresh_loop(self) -> None:
        while not self._stop.wait(SCAN_INTERVAL):
            self.update()

    def _write(self, data: dict[str, Any]) -> None:
        """Atomic: temp file in the same directory, fsync, os.replace (as sync/index.py, D8)."""
        data["updated_at"] = time.time()
        text = json.dumps(data, sort_keys=True, indent=2)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".status-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            with self._replace_lock:
                if self._stop.is_set() and not data["stopped"]:
                    return  # stop() ran meanwhile: don't overwrite the stopped state
                os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


# ---------------------------------------------------------------- reader (`meshsync status`)


def read_status(sync_dir: Path) -> dict[str, Any]:
    """Raises FileNotFoundError if absent, ValueError if unreadable."""
    path = status_path(sync_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"bad status file {path}: {e}") from e
    if not isinstance(data, dict) or not isinstance(data.get("updated_at"), int | float):
        raise ValueError(f"bad status file {path}: no updated_at")
    return data


def format_age(seconds: float) -> str:
    s = int(max(seconds, 0))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{s % 3600 // 60:02d}m"


def _clock(t: Any) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else "-"


def format_status(data: dict[str, Any], now: float) -> str:
    g = data.get
    lines = [
        f"node     {g('name', '?')} ({g('peer_id', '?')}), {g('mode', '?')}, "
        f"tcp {g('tcp_port', '?')}, udp {g('udp_port', '?')}, pid {g('pid', '?')}",
        f"started  {_clock(g('started_at'))}",
        f"updated  {_clock(g('updated_at'))} ({format_age(now - g('updated_at', now))} ago)",
        f"index    rev {g('index_rev', '?')}, {g('entries', '?')} files, "
        f"{g('tombstones', '?')} tombstones",
    ]
    peers = g("peers") or []
    if not peers:
        lines.append("peers    none")
        return "\n".join(lines)
    lines.append(f"peers    {len(peers)}")
    rows = [("NAME", "PEER_ID", "ADDRESS", "STATE", "LAST SEEN")]
    for p in peers:
        seen = p.get("last_seen")
        rows.append(
            (
                str(p.get("name", "?")),
                str(p.get("peer_id", "?")),
                f"{p.get('addr', '?')}:{p.get('tcp_port', '?')}",
                str(p.get("state", "?")),
                f"{format_age(now - seen)} ago" if seen else "-",
            )
        )
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    lines += [
        "  " + "  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)).rstrip() for r in rows
    ]
    return "\n".join(lines)


def status_report(sync_dir: Path, now: float | None = None) -> tuple[int, str]:
    """(exit code, text) for `meshsync status`: 0 running, 1 not running or unreadable."""
    now = time.time() if now is None else now
    try:
        data = read_status(sync_dir)
    except FileNotFoundError:
        return 1, "not running (no status file)"
    except (OSError, ValueError) as e:
        return 1, f"cannot read status: {e}"
    age = now - data["updated_at"]
    if data.get("stopped"):
        return 1, f"not running (stopped {format_age(age)} ago)\n{format_status(data, now)}"
    if age > STATUS_STALE_AFTER:
        return 1, f"not running (last update {format_age(age)} ago)\n{format_status(data, now)}"
    return 0, "running\n" + format_status(data, now)


# ---------------------------------------------------------------- `meshsync stats`


@dataclass
class Totals:
    count: int = 0
    bytes: int = 0

    def add(self, size: int) -> None:
        self.count += 1
        self.bytes += size


@dataclass
class Stats:
    by_type: dict[tuple[str, str], Totals] = field(default_factory=dict)  # (type, direction)
    by_direction: dict[str, Totals] = field(default_factory=dict)
    by_peer: dict[str, Totals] = field(default_factory=dict)
    total: Totals = field(default_factory=Totals)
    first_t: float | None = None
    last_t: float | None = None
    skipped: int = 0


def summarise_csv(path: Path) -> Stats:
    """Totals from a metrics CSV (#72). Malformed rows are skipped and counted.
    Raises OSError if unreadable, ValueError if the header is not CSV_HEADER."""
    stats = Stats()
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if header != CSV_HEADER.split(","):
            raise ValueError(f"{path}: expected header {CSV_HEADER!r}, got {header!r}")
        for row in reader:
            if not row:
                continue  # blank line, not a row
            try:
                t_s, direction, peer, msg_type, size_s = row
                t, size = float(t_s), int(size_s)
                if direction not in DIRECTIONS or size < 0 or not msg_type:
                    raise ValueError
            except ValueError:
                stats.skipped += 1
                continue
            stats.by_type.setdefault((msg_type, direction), Totals()).add(size)
            stats.by_direction.setdefault(direction, Totals()).add(size)
            stats.by_peer.setdefault(peer, Totals()).add(size)
            stats.total.add(size)
            stats.first_t = t if stats.first_t is None else min(stats.first_t, t)
            stats.last_t = t if stats.last_t is None else max(stats.last_t, t)
    return stats


def format_stats(s: Stats) -> str:
    if s.total.count == 0:
        out = ["no messages recorded"]
    else:
        assert s.first_t is not None and s.last_t is not None
        out = [
            f"span     {_clock(s.first_t)} to {_clock(s.last_t)} ({s.last_t - s.first_t:.1f}s)",
            f"total    {s.total.count} messages, {s.total.bytes} bytes",
            "",
            f"{'TYPE':<12}{'DIR':<6}{'COUNT':>8}{'BYTES':>14}",
        ]
        for (t, d), v in sorted(s.by_type.items()):
            out.append(f"{t:<12}{d:<6}{v.count:>8}{v.bytes:>14}")
        out += ["", f"{'DIRECTION':<18}{'COUNT':>8}{'BYTES':>14}"]
        for d, v in sorted(s.by_direction.items()):
            out.append(f"{d:<18}{v.count:>8}{v.bytes:>14}")
        out += ["", f"{'PEER':<18}{'COUNT':>8}{'BYTES':>14}"]
        for p, v in sorted(s.by_peer.items()):
            out.append(f"{p:<18}{v.count:>8}{v.bytes:>14}")
    if s.skipped:
        out.append(f"skipped {s.skipped} malformed rows")
    return "\n".join(out)
