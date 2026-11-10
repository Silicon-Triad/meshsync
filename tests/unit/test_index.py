"""Tests for sync/index.py (§7.1, D8)."""

import json
import os
import threading
from pathlib import Path

import pytest

from fakes import make_entry
from meshsync.protocol.constants import EMPTY_FILE_HASH
from meshsync.sync.index import Index


def tombstone(path: str, version: dict[str, int]) -> dict:
    return {
        "path": path,
        "size": 0,
        "file_hash": EMPTY_FILE_HASH,
        "chunks": [],
        "version": version,
        "deleted": True,
    }


def index_file(root: Path) -> Path:
    return root / ".meshsync" / "index.json"


def test_put_get_roundtrip(tmp_path: Path) -> None:
    idx = Index(tmp_path)
    e = make_entry("notes/report.txt", [b"abc", b"def"], {"A1F3C9D2": 1})
    idx.put(e)
    assert idx.get("notes/report.txt") == e


def test_get_unknown_is_none(tmp_path: Path) -> None:
    assert Index(tmp_path).get("missing.txt") is None


def test_new_index_is_empty_and_writes_nothing(tmp_path: Path) -> None:
    idx = Index(tmp_path)
    assert idx.entries() == []
    assert idx.rev == 0
    assert not index_file(tmp_path).exists()


def test_put_replaces_same_path(tmp_path: Path) -> None:
    idx = Index(tmp_path)
    idx.put(make_entry("a.txt", [b"1"], {"A1F3C9D2": 1}))
    newer = make_entry("a.txt", [b"2"], {"A1F3C9D2": 2})
    idx.put(newer)
    assert idx.get("a.txt") == newer
    assert len(idx.entries()) == 1


def test_entries_returns_all(tmp_path: Path) -> None:
    idx = Index(tmp_path)
    a = make_entry("a.txt", [b"a"], {"A1F3C9D2": 1})
    b = make_entry("dir/b.txt", [b"b"], {"B77C0E11": 1})
    idx.put(a)
    idx.put(b)
    assert sorted(idx.entries(), key=lambda e: e["path"]) == [a, b]


def test_rev_bumps_on_every_put(tmp_path: Path) -> None:
    idx = Index(tmp_path)
    e = make_entry("a.txt", [b"a"], {"A1F3C9D2": 1})
    idx.put(e)
    idx.put(e)
    assert idx.rev == 2


def test_survives_reload(tmp_path: Path) -> None:
    idx = Index(tmp_path)
    a = make_entry("a.txt", [b"a"], {"A1F3C9D2": 1})
    t = tombstone("gone.txt", {"A1F3C9D2": 3})
    idx.put(a)
    idx.put(t)
    again = Index(tmp_path)
    assert again.rev == 2
    assert again.get("a.txt") == a
    assert again.get("gone.txt") == t


def test_file_format(tmp_path: Path) -> None:
    idx = Index(tmp_path)
    e = make_entry("a.txt", [b"a"], {"A1F3C9D2": 1})
    idx.put(e)
    assert json.loads(index_file(tmp_path).read_text()) == {"rev": 1, "entries": [e]}


@pytest.mark.parametrize(
    "path", ["../escape.txt", "a/../../b", "/etc/passwd", ".meshsync/index.json", "a\\b", ""]
)
def test_rejects_bad_paths(tmp_path: Path, path: str) -> None:
    idx = Index(tmp_path)
    e = make_entry("ok.txt", [b"x"], {"A1F3C9D2": 1})
    e["path"] = path
    with pytest.raises(ValueError):
        idx.put(e)
    assert idx.entries() == []
    assert idx.rev == 0
    assert not index_file(tmp_path).exists()


def test_rejects_malformed_entry(tmp_path: Path) -> None:
    idx = Index(tmp_path)
    e = make_entry("a.txt", [b"a"], {"A1F3C9D2": 1})
    del e["deleted"]
    with pytest.raises(ValueError):
        idx.put(e)
    bad_tomb = tombstone("t.txt", {"A1F3C9D2": 1})
    bad_tomb["size"] = 5
    with pytest.raises(ValueError):
        idx.put(bad_tomb)


def test_failed_replace_keeps_old_file_and_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    idx = Index(tmp_path)
    old = make_entry("a.txt", [b"old"], {"A1F3C9D2": 1})
    idx.put(old)
    before = index_file(tmp_path).read_bytes()

    def boom(src: str, dst: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        idx.put(make_entry("a.txt", [b"new"], {"A1F3C9D2": 2}))
    monkeypatch.undo()

    assert index_file(tmp_path).read_bytes() == before
    assert idx.get("a.txt") == old
    assert idx.rev == 1
    assert [p.name for p in index_file(tmp_path).parent.iterdir()] == ["index.json"]


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "[]",
        '{"rev": 1}',
        '{"rev": -1, "entries": []}',
        '{"rev": true, "entries": []}',
        '{"rev": 0, "entries": [{"path": "../x"}]}',
    ],
)
def test_corrupt_file_raises(tmp_path: Path, content: str) -> None:
    index_file(tmp_path).parent.mkdir(parents=True)
    index_file(tmp_path).write_text(content)
    with pytest.raises(ValueError):
        Index(tmp_path)


def test_duplicate_paths_in_file_raise(tmp_path: Path) -> None:
    e = make_entry("a.txt", [b"a"], {"A1F3C9D2": 1})
    index_file(tmp_path).parent.mkdir(parents=True)
    index_file(tmp_path).write_text(json.dumps({"rev": 2, "entries": [e, e]}))
    with pytest.raises(ValueError):
        Index(tmp_path)


def test_returned_entries_are_copies(tmp_path: Path) -> None:
    idx = Index(tmp_path)
    e = make_entry("a.txt", [b"a"], {"A1F3C9D2": 1})
    idx.put(e)
    e["version"]["A1F3C9D2"] = 99
    got = idx.get("a.txt")
    assert got is not None
    got["version"]["A1F3C9D2"] = 50
    idx.entries()[0]["chunks"].clear()
    assert idx.get("a.txt") == make_entry("a.txt", [b"a"], {"A1F3C9D2": 1})


def test_concurrent_puts(tmp_path: Path) -> None:
    idx = Index(tmp_path)

    def worker(n: int) -> None:
        for i in range(10):
            idx.put(make_entry(f"t{n}/f{i}.txt", [bytes([n, i])], {"A1F3C9D2": 1}))

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert idx.rev == 40
    assert len(Index(tmp_path).entries()) == 40
