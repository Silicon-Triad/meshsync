"""Polling change detection.

Owner: Mujtaba Jawaid Rao (CS-24047)
Contract (PROJECT_CONTEXT.md §8.3):
    Poll the sync root every SCAN_INTERVAL (D13); report creates/edits/deletes.
    Skips META_DIR (.meshsync/).

Each poll walks the sync root and compares every regular file with its index entry (§7.1):
- new file, or content (file_hash) differs from the entry -> new entry, own counter bumped;
- entry exists (not a tombstone) but the file is gone -> tombstone (§7.3, PROTOCOL.md §9.5).
Renames are seen as a delete plus a create (D14). Files whose size and mtime did not change since
the last poll are not re-read. That size/mtime cache is saved to META_DIR/scan_cache.json, so a
restart does not rechunk unchanged files either (#51); on load, a cached file is kept only if its
index entry still has the cached file_hash, so the chunk list reused is the index's own (§7.1).
Content equal to the index entry (for example a file the engine
just assembled) is not a change, so remote writes are not echoed back as local edits.

A changed file's chunks are put in the chunk store, so peers can fetch them with CHUNK_REQ, then
the entry is stored in the index and passed to on_change (node.ScannerLike).

`lock` is shared with the engine (build_node sets both to one lock, issue #14). The scanner holds
it only across index.get, the compare and index.put; the slow chunking and hashing run outside
it. Under the lock the file is stat'ed again, so a file the engine replaced after we read it is
not reported as a local edit, and a file the engine created after the walk is not tombstoned.
"""

import hashlib
import json
import logging
import os
import tempfile
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

_CACHE_FILE = "scan_cache.json"  # local only, never sent to peers (#51)


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
        self._cache_path = self.root / META_DIR / _CACHE_FILE
        # path -> (size, mtime_ns, file_hash) when last scanned; persisted across restarts (#51)
        self._seen: dict[str, tuple[int, int, str]] = self._load_cache()
        self.lock: threading.RLock = threading.RLock()  # shared with the engine (#14)
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
        before = dict(self._seen)
        for rel, st in self._walk():
            present.add(rel)
            key = (st.st_size, st.st_mtime_ns)
            cached = self._seen.get(rel)
            if cached is not None and cached[:2] == key:
                continue
            result = self._scan_file(rel, st)
            if result is None:
                continue
            entry, file_hash = result
            self._seen[rel] = (*key, file_hash)
            if entry is not _UNCHANGED:
                changed.append(entry)
        for path in list(self._seen):
            if path not in present:
                del self._seen[path]
        if self._seen != before:
            self._save_cache()
        with self.lock:
            for old in self.index.entries():
                if old["deleted"] or old["path"] in present:
                    continue
                if os.path.lexists(self.root / old["path"]):
                    continue  # appeared after the walk (e.g. the engine wrote it): next poll
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

    def _scan_file(self, rel: str, st: os.stat_result) -> "tuple[dict | object, str] | None":
        """(new entry, or _UNCHANGED, and the file_hash read), or None to retry next poll."""
        try:
            size, file_hash, chunks = self._read(rel)  # slow: outside the lock
        except (OSError, _FileChanged) as e:
            log.info("skipping %s this poll: %s", rel, e)
            return None
        with self.lock:
            if not _same_file(self.root / rel, st):
                # Replaced while we read it (e.g. by the engine): what we read is stale.
                return None
            old = self.index.get(rel)
            if old is not None and not old["deleted"] and old["file_hash"] == file_hash:
                return _UNCHANGED, file_hash
            entry = self._make_entry(rel, size, file_hash, chunks, deleted=False)
            # A rejected path is remembered as seen, so it is not re-read and logged every poll.
            return (entry if self._put(entry) else _UNCHANGED), file_hash

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

    # ------------------------------------------------------------ size/mtime cache (#51)

    def _load_cache(self) -> dict[str, tuple[int, int, str]]:
        """Cached (size, mtime_ns, file_hash) per path; empty (full rescan) if missing or bad."""
        try:
            raw = self._cache_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as e:
            log.warning("cannot read scan cache %s, rescanning all files: %s", self._cache_path, e)
            return {}
        try:
            data = json.loads(raw)
            if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
                raise ValueError("expected an object with a 'files' object")
            seen: dict[str, tuple[int, int, str]] = {}
            for path, item in data["files"].items():
                if not (
                    isinstance(item, list)
                    and len(item) == 3
                    and all(type(x) is int for x in item[:2])
                    and isinstance(item[2], str)
                ):
                    raise ValueError(f"bad cache item for {path!r}")
                seen[path] = (item[0], item[1], item[2])
        except ValueError as e:  # json.JSONDecodeError is a ValueError
            log.warning("scan cache %s is invalid, rescanning all files: %s", self._cache_path, e)
            return {}
        # Keep a cached file only if the index still describes the same content (§7.1); otherwise
        # it is re-read, so the cache never hides a file the index does not match.
        kept: dict[str, tuple[int, int, str]] = {}
        for path, item in seen.items():
            entry = self.index.get(path)
            if entry is not None and not entry["deleted"] and entry["file_hash"] == item[2]:
                kept[path] = item
        return kept

    def _save_cache(self) -> None:
        """Write the cache atomically (temp file + os.replace, as the index does, D8)."""
        payload = json.dumps({"files": {p: list(v) for p, v in self._seen.items()}}, sort_keys=True)
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self._cache_path.parent, prefix=".tmp-scan-cache-")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(payload)
                os.replace(tmp, self._cache_path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except FileNotFoundError:
                    pass
                raise
        except OSError as e:  # the cache is an optimisation; losing it only costs a rescan
            log.warning("cannot save scan cache %s: %s", self._cache_path, e)


_UNCHANGED = object()


def _same_file(path: Path, st: os.stat_result) -> bool:
    """True if path is still the file described by st (not replaced or modified)."""
    try:
        now = path.lstat()
    except FileNotFoundError:
        return False
    return (now.st_ino, now.st_size, now.st_mtime_ns) == (st.st_ino, st.st_size, st.st_mtime_ns)
