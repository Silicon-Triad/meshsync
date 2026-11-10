"""Polling change detection.

Owner: Mujtaba Jawaid Rao (CS-24047)
Contract (PROJECT_CONTEXT.md §8.3):
    Poll the sync root every SCAN_INTERVAL (D13); report creates/edits/deletes.
    Skips META_DIR (.meshsync/).

Each poll walks the sync root and compares every regular file with its index entry (§7.1):
- new file, or content (file_hash) differs from the entry -> new entry, own counter bumped;
- entry exists (not a tombstone) but the file is gone -> tombstone (§7.3, PROTOCOL.md §9.5).
Renames are seen as a delete plus a create (D14). Files whose size and mtime did not change since
the last poll are not re-read. Content equal to the index entry (for example a file the engine
just assembled) is not a change, so remote writes are not echoed back as local edits.

A changed file's chunks are put in the chunk store, so peers can fetch them with CHUNK_REQ, then
the entry is stored in the index and passed to on_change (node.ScannerLike).
"""

import hashlib
import logging
import os
import threading
from collections.abc import Callable
from pathlib import Path

from meshsync.config import Config
from meshsync.protocol.constants import EMPTY_FILE_HASH, META_DIR, SCAN_INTERVAL
from meshsync.sync import vclock
from meshsync.sync.chunker import chunk_file
from meshsync.sync.index import Index
from meshsync.sync.store import ChunkStore

log = logging.getLogger(__name__)


class _FileChanged(Exception):
    """The file changed while it was being read; try again on the next poll."""


class Scanner:
    def __init__(
        self,
        config: Config,
        index: Index,
        *,
        peer_id: str | None = None,
        interval: float = SCAN_INTERVAL,
    ) -> None:
        self.root = Path(config.sync_dir)
        self.index = index
        self.mode = config.chunking
        self.interval = interval
        if peer_id is None:
            # Same file Node uses (decision (l)); imported here to avoid an import cycle.
            from meshsync.node import load_or_create_peer_id

            peer_id = load_or_create_peer_id(self.root)
        self.peer_id = peer_id
        self.store = ChunkStore(self.root)
        self._seen: dict[str, tuple[int, int]] = {}  # path -> (size, mtime_ns) last scanned
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._on_change: Callable[[dict], None] | None = None

    # ------------------------------------------------------------ lifecycle (node.ScannerLike)

    def start(self, on_change: Callable[[dict], None]) -> None:
        if self._thread is not None:
            return
        self._on_change = on_change
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="scanner", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join()
        self._thread = None

    def _run(self) -> None:
        while True:
            try:
                for entry in self.scan_once():
                    self._notify(entry)
            except Exception:  # noqa: BLE001 - one bad poll must not kill the scanner
                log.exception("scan of %s failed", self.root)
            if self._stop.wait(self.interval):  # D13: poll every SCAN_INTERVAL
                return

    def _notify(self, entry: dict) -> None:
        if self._on_change is None:
            return
        try:
            self._on_change(entry)
        except Exception:  # noqa: BLE001 - a failing callback must not stop scanning
            log.exception("on_change failed for %s", entry["path"])

    # ------------------------------------------------------------ one poll

    def scan_once(self) -> list[dict]:
        """Scan the root once; store and return the entries that changed (no callback)."""
        changed: list[dict] = []
        present: set[str] = set()
        for rel, st in self._walk():
            present.add(rel)
            key = (st.st_size, st.st_mtime_ns)
            if self._seen.get(rel) == key:
                continue
            entry = self._scan_file(rel)
            if entry is None:
                continue
            self._seen[rel] = key
            if entry is not _UNCHANGED:
                changed.append(entry)
        for path in list(self._seen):
            if path not in present:
                del self._seen[path]
        for old in self.index.entries():
            if not old["deleted"] and old["path"] not in present:
                entry = self._make_entry(old["path"], 0, EMPTY_FILE_HASH, [], deleted=True)
                if self._put(entry):
                    changed.append(entry)
        return changed

    def _walk(self) -> list[tuple[str, os.stat_result]]:
        """(posix relative path, stat) of every regular file, skipping META_DIR and symlinks."""
        found: list[tuple[str, os.stat_result]] = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            rel_dir = Path(dirpath).relative_to(self.root)
            if rel_dir == Path("."):
                dirnames[:] = [d for d in dirnames if d != META_DIR]  # §7.3
            dirnames.sort()
            for name in sorted(filenames):
                full = Path(dirpath) / name
                try:
                    st = full.lstat()
                except FileNotFoundError:
                    continue
                if not full.is_symlink() and full.is_file():
                    found.append(((rel_dir / name).as_posix(), st))
        return found

    def _scan_file(self, rel: str) -> "dict | object | None":
        """New entry if the file changed, _UNCHANGED if not, None to retry next poll."""
        try:
            size, file_hash, chunks = self._read(rel)
        except (OSError, _FileChanged) as e:
            log.info("skipping %s this poll: %s", rel, e)
            return None
        old = self.index.get(rel)
        if old is not None and not old["deleted"] and old["file_hash"] == file_hash:
            return _UNCHANGED
        entry = self._make_entry(rel, size, file_hash, chunks, deleted=False)
        # A rejected path is remembered as seen, so it is not re-read and logged every poll.
        return entry if self._put(entry) else _UNCHANGED

    def _read(self, rel: str) -> tuple[int, str, list[str]]:
        """Chunk the file, put its chunks in the store and return (size, file_hash, chunks)."""
        path = self.root / rel
        spans = list(chunk_file(path, self.mode))  # D10
        whole = hashlib.sha256()
        size = 0
        with open(path, "rb") as f:
            for offset, length, h in spans:
                f.seek(offset)
                data = f.read(length)
                if len(data) != length or hashlib.sha256(data).hexdigest() != h:
                    raise _FileChanged("file changed while reading")
                whole.update(data)
                size += length
                if not self.store.has(h):
                    self.store.put(h, data)
            if f.read(1):
                raise _FileChanged("file grew while reading")
        return size, whole.hexdigest(), [h for _, _, h in spans]

    def _make_entry(
        self, rel: str, size: int, file_hash: str, chunks: list[str], *, deleted: bool
    ) -> dict:
        old = self.index.get(rel)
        version = old["version"] if old is not None else {}
        return {
            "path": rel,
            "size": size,
            "file_hash": file_hash,
            "chunks": chunks,
            "version": vclock.bump(version, self.peer_id),  # §7.3: local edit bumps own counter
            "deleted": deleted,
        }

    def _put(self, entry: dict) -> bool:
        try:
            self.index.put(entry)
        except ValueError as e:  # e.g. a file name that is not a valid wire path (§7.1)
            log.warning("not syncing %s: %s", entry["path"], e)
            return False
        return True


_UNCHANGED = object()
