"""Tests for transfer/assembler.py (#16, §7.3)."""

import hashlib
from pathlib import Path

import pytest

from meshsync.protocol.constants import EMPTY_FILE_HASH, META_DIR
from meshsync.sync.store import ChunkStore
from meshsync.transfer.assembler import assemble


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_entry(store: ChunkStore, path: str, parts: list[bytes], *, put: bool = True) -> dict:
    if put:
        for p in parts:
            store.put(sha(p), p)
    content = b"".join(parts)
    return {
        "path": path,
        "size": len(content),
        "file_hash": sha(content),
        "chunks": [sha(p) for p in parts],
        "version": {"A1F3C9D2": 1},
        "deleted": False,
    }


def tmp_files(root: Path) -> list[Path]:
    d = root / META_DIR / "tmp"
    return list(d.iterdir()) if d.exists() else []


@pytest.fixture
def store(tmp_path: Path) -> ChunkStore:
    return ChunkStore(tmp_path)


def test_assembles_chunks_in_order(tmp_path: Path, store: ChunkStore) -> None:
    entry = make_entry(store, "notes/sub/report.txt", [b"alpha-", b"beta-", b"gamma"])
    out = assemble(tmp_path, entry, store)
    assert out == tmp_path / "notes/sub/report.txt"
    assert out.read_bytes() == b"alpha-beta-gamma"
    assert tmp_files(tmp_path) == []


def test_repeated_chunk(tmp_path: Path, store: ChunkStore) -> None:
    entry = make_entry(store, "r.bin", [b"xx", b"yy", b"xx"])
    assert assemble(tmp_path, entry, store).read_bytes() == b"xxyyxx"


def test_empty_file(tmp_path: Path, store: ChunkStore) -> None:
    entry = make_entry(store, "empty.txt", [])
    assert entry["file_hash"] == EMPTY_FILE_HASH
    assert assemble(tmp_path, entry, store).read_bytes() == b""


def test_overwrites_existing_file(tmp_path: Path, store: ChunkStore) -> None:
    (tmp_path / "a.txt").write_bytes(b"old")
    entry = make_entry(store, "a.txt", [b"new content"])
    assemble(tmp_path, entry, store)
    assert (tmp_path / "a.txt").read_bytes() == b"new content"


def test_dest_writes_elsewhere_and_leaves_original(tmp_path: Path, store: ChunkStore) -> None:
    # PROTOCOL.md §9.4: the engine writes the conflict copy under another name
    (tmp_path / "report.txt").write_bytes(b"local version")
    entry = make_entry(store, "report.txt", [b"remote version"])
    out = assemble(tmp_path, entry, store, dest="report.conflict-A1F3C9D2.txt")
    assert out == tmp_path / "report.conflict-A1F3C9D2.txt"
    assert out.read_bytes() == b"remote version"
    assert (tmp_path / "report.txt").read_bytes() == b"local version"


def test_missing_chunk_leaves_target_untouched(tmp_path: Path, store: ChunkStore) -> None:
    (tmp_path / "a.txt").write_bytes(b"old")
    entry = make_entry(store, "a.txt", [b"have", b"missing"], put=False)
    store.put(sha(b"have"), b"have")
    with pytest.raises(KeyError):
        assemble(tmp_path, entry, store)
    assert (tmp_path / "a.txt").read_bytes() == b"old"
    assert tmp_files(tmp_path) == []


def test_wrong_file_hash_rejected(tmp_path: Path, store: ChunkStore) -> None:
    entry = make_entry(store, "new.txt", [b"data"])
    entry["file_hash"] = sha(b"other")
    with pytest.raises(ValueError, match="file_hash"):
        assemble(tmp_path, entry, store)
    assert not (tmp_path / "new.txt").exists()
    assert tmp_files(tmp_path) == []


def test_wrong_size_rejected(tmp_path: Path, store: ChunkStore) -> None:
    entry = make_entry(store, "new.txt", [b"data"])
    entry["size"] = 5
    with pytest.raises(ValueError, match="bytes"):
        assemble(tmp_path, entry, store)
    assert not (tmp_path / "new.txt").exists()
    assert tmp_files(tmp_path) == []


def test_tombstone_rejected(tmp_path: Path, store: ChunkStore) -> None:
    entry = make_entry(store, "gone.txt", [])
    entry["deleted"] = True
    with pytest.raises(ValueError, match="tombstone"):
        assemble(tmp_path, entry, store)
    assert not (tmp_path / "gone.txt").exists()


@pytest.mark.parametrize(
    "bad",
    ["../escape.txt", "a/../../b", "/etc/passwd", "a//b", f"{META_DIR}/chunks/x", ""],
)
def test_unsafe_path_rejected(tmp_path: Path, store: ChunkStore, bad: str) -> None:
    entry = make_entry(store, "ok.txt", [b"x"])
    with pytest.raises(ValueError):
        assemble(tmp_path, entry, store, dest=bad)
    entry["path"] = bad
    with pytest.raises(ValueError):
        assemble(tmp_path, entry, store)
    assert not (tmp_path.parent / "escape.txt").exists()
    assert tmp_files(tmp_path) == []
