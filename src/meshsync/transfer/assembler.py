"""File assembly from verified chunks.

Owner: Huzaifa Hanif (CS-24039)
Contract (PROJECT_CONTEXT.md §8.3):
    Build in <root>/.meshsync/tmp/, verify file_hash, os.replace into place (§7.3).
    Never expose partial files.

    assemble(root, entry, store, dest=None) -> Path
        Writes the file described by `entry` (§7.1) from chunks already in `store`. `dest` is a
        POSIX path relative to `root` and defaults to entry["path"]; the engine passes the
        conflict-copy name here (PROTOCOL.md §9.4). Returns the absolute path written.
        Raises KeyError if a chunk is missing from the store, ValueError if the entry is a
        tombstone, the path is unsafe or the result does not match size/file_hash. On any
        error nothing is left at `dest` that was not there before, and the temp file is removed.

Called by the sync engine after Scheduler.fetch() has put every chunk in the store (#14, #15).
"""

import hashlib
import logging
import os
import tempfile
from pathlib import Path
from typing import Protocol

from meshsync.protocol.constants import META_DIR
from meshsync.protocol.framing import ProtocolError
from meshsync.protocol.messages import validate_path

log = logging.getLogger(__name__)

_TMP_DIR = "tmp"  # path segment fixed by §7.3 / PROTOCOL.md §9.8


class ChunkSource(Protocol):
    """The part of sync.store.ChunkStore the assembler needs."""

    def get(self, h: str) -> bytes: ...


def _safe_relpath(path: str) -> str:
    # §7.1, PROTOCOL.md §9.2: same rules as a received entry; no "..", no absolute path,
    # nothing inside .meshsync/. Re-raised as ValueError: this is a local caller error.
    try:
        return validate_path(path)
    except ProtocolError as e:
        raise ValueError(str(e)) from None


def assemble(
    root: str | os.PathLike[str],
    entry: dict,
    store: ChunkSource,
    dest: str | None = None,
) -> Path:
    """Build entry's file from stored chunks and atomically move it to root/dest (§7.3)."""
    if entry.get("deleted"):
        raise ValueError(f"cannot assemble a tombstone: {entry.get('path')!r}")
    rel = _safe_relpath(entry["path"] if dest is None else dest)
    root = Path(root)
    target = root / rel
    tmp_dir = root / META_DIR / _TMP_DIR
    tmp_dir.mkdir(parents=True, exist_ok=True)

    # Temp file under .meshsync/tmp/ (excluded from scanning, §7.3), same filesystem as the
    # target so os.replace is atomic: readers see the old file or the complete new one.
    fd, tmp = tempfile.mkstemp(dir=tmp_dir, prefix="asm-")
    try:
        digest = hashlib.sha256()
        size = 0
        with os.fdopen(fd, "wb") as f:
            for h in entry["chunks"]:
                data = store.get(h)  # KeyError if missing; ChunkStore.put already verified it
                f.write(data)
                digest.update(data)
                size += len(data)
            # Durable before the rename, like ChunkStore.put: never a short file at target.
            f.flush()
            os.fsync(f.fileno())
        if size != entry["size"]:
            raise ValueError(f"{rel}: assembled {size} bytes, entry says {entry['size']}")
        if digest.hexdigest() != entry["file_hash"]:  # §7.3: verify file_hash
            raise ValueError(f"{rel}: assembled content does not match file_hash")
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    log.info("assembled %s (%d bytes, %d chunks)", rel, size, len(entry["chunks"]))
    return target
