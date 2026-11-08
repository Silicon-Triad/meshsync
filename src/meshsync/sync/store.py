"""Content-addressed chunk store.

Owner: Mujtaba Jawaid Rao (CS-24047)
Contract (PROJECT_CONTEXT.md §8.3):
    ChunkStore.has(hash) / get(hash) / put(hash, data)
    Layout <root>/.meshsync/chunks/<hash[:2]>/<hash> (§7.3).

Hashes are 64 lowercase hex characters (SHA-256, PROTOCOL.md decision (d)); anything else raises
ValueError, so a hash can never name a path outside the store. put() verifies the data against
its hash and writes atomically (temp file + os.replace), so readers never see a partial chunk.
"""
# FROZEN INTERFACE (AGENTS.md §6): changing or adding to the public interface in this file
# needs an issue and approval from all three members first (R-6.2).

import hashlib
import logging
import os
import re
import tempfile
from pathlib import Path

from meshsync.protocol.constants import META_DIR

log = logging.getLogger(__name__)

_CHUNKS_DIR = "chunks"  # path segment fixed by §7.3
_HASH_RE = re.compile(r"[0-9a-f]{64}")


def _check_hash(h: str) -> None:
    if not isinstance(h, str) or not _HASH_RE.fullmatch(h):
        raise ValueError(f"invalid chunk hash: {h!r}")


class ChunkStore:
    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.dir = Path(root) / META_DIR / _CHUNKS_DIR

    def _path(self, h: str) -> Path:
        _check_hash(h)
        return self.dir / h[:2] / h

    def has(self, h: str) -> bool:
        return self._path(h).is_file()

    def get(self, h: str) -> bytes:
        """Chunk bytes. Raises KeyError if absent (node.py CHUNK_REQ -> ERROR CHUNK_NOT_FOUND)."""
        path = self._path(h)
        try:
            return path.read_bytes()
        except FileNotFoundError:
            raise KeyError(h) from None

    def put(self, h: str, data: bytes) -> None:
        """Store data under h. ValueError if sha256(data) != h; no-op if already stored."""
        path = self._path(h)
        if hashlib.sha256(data).hexdigest() != h:  # §6.4: chunks are verified by SHA-256
            raise ValueError(f"data does not match chunk hash {h}")
        if path.is_file():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        # Temp file in the same directory so os.replace is atomic (§7.3: no partial files).
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise
        log.debug("stored chunk %s (%d bytes)", h, len(data))
