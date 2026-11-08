"""Tests for sync/store.py (§7.3, §6.4)."""

import hashlib
import threading
from pathlib import Path

import pytest

from meshsync.sync.store import ChunkStore


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_put_get_has_roundtrip(tmp_path: Path) -> None:
    store = ChunkStore(tmp_path)
    data = b"hello chunk"
    h = sha(data)
    assert not store.has(h)
    store.put(h, data)
    assert store.has(h)
    assert store.get(h) == data


def test_layout(tmp_path: Path) -> None:
    data = b"layout"
    h = sha(data)
    ChunkStore(tmp_path).put(h, data)
    assert (tmp_path / ".meshsync" / "chunks" / h[:2] / h).read_bytes() == data


def test_empty_chunk(tmp_path: Path) -> None:
    store = ChunkStore(tmp_path)
    store.put(sha(b""), b"")
    assert store.get(sha(b"")) == b""


def test_get_missing_raises_keyerror(tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        ChunkStore(tmp_path).get(sha(b"absent"))


def test_put_twice_is_noop(tmp_path: Path) -> None:
    store = ChunkStore(tmp_path)
    h = sha(b"x")
    store.put(h, b"x")
    store.put(h, b"x")
    assert store.get(h) == b"x"


def test_put_rejects_mismatched_data(tmp_path: Path) -> None:
    store = ChunkStore(tmp_path)
    h = sha(b"right")
    with pytest.raises(ValueError):
        store.put(h, b"wrong")
    assert not store.has(h)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "abc",
        sha(b"x").upper(),  # decision (d): hashes are lowercase
        "../" + "a" * 61,
        "g" * 64,
        sha(b"x") + "0",
    ],
)
def test_invalid_hash_rejected(tmp_path: Path, bad: str) -> None:
    store = ChunkStore(tmp_path)
    for call in (lambda: store.has(bad), lambda: store.get(bad), lambda: store.put(bad, b"")):
        with pytest.raises(ValueError):
            call()


def test_no_temp_files_left(tmp_path: Path) -> None:
    store = ChunkStore(tmp_path)
    for i in range(5):
        data = bytes([i]) * 100
        store.put(sha(data), data)
    leftovers = [p for p in (tmp_path / ".meshsync").rglob(".tmp-*")]
    assert leftovers == []


def test_concurrent_puts_same_chunk(tmp_path: Path) -> None:
    store = ChunkStore(tmp_path)
    data = b"z" * 100_000
    h = sha(data)
    threads = [threading.Thread(target=store.put, args=(h, data)) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert store.get(h) == data
    assert list((tmp_path / ".meshsync" / "chunks" / h[:2]).iterdir()) == [
        tmp_path / ".meshsync" / "chunks" / h[:2] / h
    ]
