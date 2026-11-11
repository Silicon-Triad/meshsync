# FROZEN INTERFACE (AGENTS.md §6): changing or adding to the public interface in this file
# needs an issue and approval from all three members first (R-6.2).
"""Local file index.

Owner: Mujtaba Jawaid Rao (CS-24047)
Contract (PROJECT_CONTEXT.md §8.3):
    Index.get(path) / Index.put(entry) / Index.entries()
    Entry shape §7.1; JSON file saved atomically (temp + os.replace, D8).
    Index.rev: counter that goes up on every put (node.IndexLike; sent in ANNOUNCE and INDEX).

The index lives in <root>/.meshsync/index.json as {"rev": N, "entries": [entry, ...]}. The file is
local only and never sent on the wire. Every entry is validated with the same checks used on
received entries (protocol.messages.validate_entry), so a path containing "..", an absolute path
or a path inside .meshsync/ is rejected (§7.1) with ValueError and nothing is written.

A corrupt or invalid index file raises ValueError on load instead of starting empty, so a bad
file is never silently replaced by an empty index.
"""

import copy
import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from meshsync.protocol.constants import META_DIR
from meshsync.protocol.framing import ProtocolError
from meshsync.protocol.messages import validate_entry

log = logging.getLogger(__name__)

_INDEX_FILE = "index.json"  # local file name; not part of the wire protocol


def _check_entry(entry: Any) -> None:
    """ValueError unless entry is a valid §7.1 entry (PROTOCOL.md §9.1, §9.2)."""
    try:
        validate_entry(entry)
    except ProtocolError as e:
        raise ValueError(str(e)) from None


class Index:
    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.path = Path(root) / META_DIR / _INDEX_FILE
        self._lock = threading.Lock()
        self._files: dict[str, dict] = {}
        self._rev = 0
        self._load()

    @property
    def rev(self) -> int:
        with self._lock:
            return self._rev

    def get(self, path: str) -> dict | None:
        """Copy of the entry for path, or None if the index has none."""
        with self._lock:
            entry = self._files.get(path)
            return copy.deepcopy(entry) if entry is not None else None

    def put(self, entry: dict) -> None:
        """Validate, store and save entry (replacing any entry with the same path); bump rev."""
        _check_entry(entry)
        entry = copy.deepcopy(entry)
        with self._lock:
            files = dict(self._files)
            files[entry["path"]] = entry
            # Save before updating memory: if the write fails, memory and disk still agree.
            self._save(files, self._rev + 1)
            self._files = files
            self._rev += 1

    def entries(self) -> list[dict]:
        """Copies of all entries."""
        with self._lock:
            return copy.deepcopy(list(self._files.values()))

    # ------------------------------------------------------------------ persistence (D8)

    def _load(self) -> None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        try:
            data = json.loads(raw)
            if not isinstance(data, dict) or set(data) != {"rev", "entries"}:
                raise ValueError("expected an object with keys 'rev' and 'entries'")
            rev, entries = data["rev"], data["entries"]
            if isinstance(rev, bool) or not isinstance(rev, int) or rev < 0:
                raise ValueError("rev must be an integer >= 0")
            if not isinstance(entries, list):
                raise ValueError("entries must be a list")
            files: dict[str, dict] = {}
            for entry in entries:
                _check_entry(entry)
                if entry["path"] in files:
                    raise ValueError(f"duplicate path {entry['path']!r}")
                files[entry["path"]] = entry
        except ValueError as e:  # json.JSONDecodeError is a ValueError
            log.error("index file %s is invalid: %s", self.path, e)
            raise ValueError(f"invalid index file {self.path}: {e}") from None
        self._files, self._rev = files, rev
        log.debug("loaded index rev %d with %d entries", rev, len(files))

    def _save(self, files: dict[str, dict], rev: int) -> None:
        """Write atomically: temp file in the same directory, fsync, os.replace (D8)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"rev": rev, "entries": list(files.values())}, sort_keys=True, allow_nan=False
        )
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".tmp-index-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                # Durable before the rename, so a crash never leaves a short index file.
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise
