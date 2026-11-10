"""Tests for sync/scanner.py (D13, D14, §7.3, §8.3)."""

import hashlib
import os
import threading
from pathlib import Path

import pytest

from meshsync.config import Config
from meshsync.protocol.constants import EMPTY_FILE_HASH
from meshsync.sync.index import Index
from meshsync.sync.scanner import Scanner
from meshsync.sync.store import ChunkStore

ME = "AAAA0001"
OTHER = "BBBB0002"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make(tmp_path: Path, **kw: object) -> tuple[Scanner, Index]:
    cfg = Config(sync_dir=tmp_path, name="me", secret_file=tmp_path / "secret")
    index = Index(tmp_path)
    return Scanner(cfg, index, peer_id=ME, **kw), index  # type: ignore[arg-type]


def touch_later(path: Path) -> None:
    """Move mtime forward so a same-size rewrite is noticed even on coarse clocks."""
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))


def test_create_detected(tmp_path: Path) -> None:
    scanner, index = make(tmp_path)
    (tmp_path / "a.txt").write_bytes(b"hello")
    [entry] = scanner.scan_once()
    assert entry == {
        "path": "a.txt",
        "size": 5,
        "file_hash": sha(b"hello"),
        "chunks": [sha(b"hello")],
        "version": {ME: 1},
        "deleted": False,
    }
    assert index.get("a.txt") == entry


def test_chunks_put_in_store(tmp_path: Path) -> None:
    scanner, _ = make(tmp_path)
    data = os.urandom(300 * 1024)
    (tmp_path / "big.bin").write_bytes(data)
    [entry] = scanner.scan_once()
    store = ChunkStore(tmp_path)
    assert b"".join(store.get(h) for h in entry["chunks"]) == data
    assert entry["file_hash"] == sha(data)
    assert entry["size"] == len(data)


def test_no_change_no_entry(tmp_path: Path) -> None:
    scanner, index = make(tmp_path)
    (tmp_path / "a.txt").write_bytes(b"x")
    scanner.scan_once()
    rev = index.rev
    assert scanner.scan_once() == []
    assert index.rev == rev


def test_edit_bumps_own_counter(tmp_path: Path) -> None:
    scanner, _ = make(tmp_path)
    f = tmp_path / "a.txt"
    f.write_bytes(b"one")
    scanner.scan_once()
    f.write_bytes(b"two")
    touch_later(f)
    [entry] = scanner.scan_once()
    assert entry["file_hash"] == sha(b"two")
    assert entry["version"] == {ME: 2}


def test_edit_keeps_other_counters(tmp_path: Path) -> None:
    scanner, index = make(tmp_path)
    f = tmp_path / "a.txt"
    f.write_bytes(b"remote")
    index.put(
        {
            "path": "a.txt",
            "size": 6,
            "file_hash": sha(b"remote"),
            "chunks": [sha(b"remote")],
            "version": {OTHER: 3},
            "deleted": False,
        }
    )
    assert scanner.scan_once() == []  # same content as the index: not a local edit
    f.write_bytes(b"local!")
    touch_later(f)
    [entry] = scanner.scan_once()
    assert entry["version"] == {OTHER: 3, ME: 1}


def test_delete_makes_tombstone(tmp_path: Path) -> None:
    scanner, index = make(tmp_path)
    f = tmp_path / "a.txt"
    f.write_bytes(b"bye")
    scanner.scan_once()
    f.unlink()
    [entry] = scanner.scan_once()
    assert entry == {
        "path": "a.txt",
        "size": 0,
        "file_hash": EMPTY_FILE_HASH,
        "chunks": [],
        "version": {ME: 2},
        "deleted": True,
    }
    assert scanner.scan_once() == []  # tombstone is not re-created
    assert index.get("a.txt") == entry


def test_recreate_after_delete(tmp_path: Path) -> None:
    scanner, _ = make(tmp_path)
    f = tmp_path / "a.txt"
    f.write_bytes(b"v")
    scanner.scan_once()
    f.unlink()
    scanner.scan_once()
    f.write_bytes(b"v")
    [entry] = scanner.scan_once()
    assert entry["deleted"] is False
    assert entry["version"] == {ME: 3}


def test_rename_is_delete_plus_create(tmp_path: Path) -> None:
    scanner, _ = make(tmp_path)
    (tmp_path / "old.txt").write_bytes(b"same")
    scanner.scan_once()
    (tmp_path / "old.txt").rename(tmp_path / "new.txt")
    changed = {e["path"]: e for e in scanner.scan_once()}  # D14
    assert changed["new.txt"]["deleted"] is False
    assert changed["old.txt"]["deleted"] is True


def test_subdirectories(tmp_path: Path) -> None:
    scanner, _ = make(tmp_path)
    (tmp_path / "d" / "e").mkdir(parents=True)
    (tmp_path / "d" / "e" / "f.txt").write_bytes(b"deep")
    [entry] = scanner.scan_once()
    assert entry["path"] == "d/e/f.txt"


def test_meta_dir_skipped(tmp_path: Path) -> None:
    scanner, index = make(tmp_path)
    (tmp_path / ".meshsync" / "tmp").mkdir(parents=True)
    (tmp_path / ".meshsync" / "tmp" / "partial").write_bytes(b"x")
    assert scanner.scan_once() == []
    assert index.entries() == []


def test_nested_meshsync_name_is_synced(tmp_path: Path) -> None:
    # Only the top-level .meshsync is metadata (PROTOCOL.md §9.2).
    scanner, _ = make(tmp_path)
    (tmp_path / "sub" / ".meshsync").mkdir(parents=True)
    (tmp_path / "sub" / ".meshsync" / "x").write_bytes(b"x")
    [entry] = scanner.scan_once()
    assert entry["path"] == "sub/.meshsync/x"


def test_symlink_skipped(tmp_path: Path) -> None:
    scanner, _ = make(tmp_path)
    (tmp_path / "real.txt").write_bytes(b"r")
    (tmp_path / "link.txt").symlink_to(tmp_path / "real.txt")
    assert [e["path"] for e in scanner.scan_once()] == ["real.txt"]


def test_invalid_path_skipped_once(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    scanner, index = make(tmp_path)
    (tmp_path / "bad\x01name").write_bytes(b"x")
    assert scanner.scan_once() == []
    assert index.entries() == []
    caplog.clear()
    scanner.scan_once()
    assert "not syncing" not in caplog.text  # not re-read and re-logged every poll


def test_empty_file(tmp_path: Path) -> None:
    scanner, _ = make(tmp_path)
    (tmp_path / "e").write_bytes(b"")
    [entry] = scanner.scan_once()
    assert entry["size"] == 0
    assert entry["chunks"] == []
    assert entry["file_hash"] == EMPTY_FILE_HASH


def test_start_calls_on_change_and_stop(tmp_path: Path) -> None:
    scanner, _ = make(tmp_path, interval=0.01)
    got: list[dict] = []
    seen = threading.Event()

    def on_change(entry: dict) -> None:
        got.append(entry)
        seen.set()

    (tmp_path / "a.txt").write_bytes(b"x")
    scanner.start(on_change)
    try:
        assert seen.wait(5)
    finally:
        scanner.stop()
    assert got[0]["path"] == "a.txt"


def test_callback_error_does_not_stop_scanner(tmp_path: Path) -> None:
    scanner, _ = make(tmp_path, interval=0.01)
    calls: list[str] = []
    second = threading.Event()

    def on_change(entry: dict) -> None:
        calls.append(entry["path"])
        if len(calls) == 1:
            (tmp_path / "b.txt").write_bytes(b"y")
            raise RuntimeError("boom")
        second.set()

    (tmp_path / "a.txt").write_bytes(b"x")
    scanner.start(on_change)
    try:
        assert second.wait(5)
    finally:
        scanner.stop()
    assert calls[:2] == ["a.txt", "b.txt"]


def test_peer_id_from_meta_dir(tmp_path: Path) -> None:
    from meshsync.node import load_or_create_peer_id

    cfg = Config(sync_dir=tmp_path, name="me", secret_file=tmp_path / "secret")
    scanner = Scanner(cfg, Index(tmp_path))
    assert scanner.peer_id == load_or_create_peer_id(tmp_path)
