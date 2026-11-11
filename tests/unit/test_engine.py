"""Tests for sync/engine.py (§7.2, §7.3, PROTOCOL.md §9.3-§9.5, issue #14)."""

import hashlib
import threading
from pathlib import Path

import pytest

from meshsync.config import Config
from meshsync.node import Action
from meshsync.protocol.constants import EMPTY_FILE_HASH
from meshsync.sync.engine import Engine, conflict_name, resolve
from meshsync.sync.index import Index
from meshsync.sync.store import ChunkStore

A = "AAAA0001"  # lowest
B = "BBBB0002"
C = "CCCC0003"  # highest


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fake_assemble(root: Path, entry: dict, store: ChunkStore, dest: str | None = None) -> Path:
    """Stand-in for transfer/assembler.assemble (#52) with the same contract."""
    if entry["deleted"]:
        raise ValueError("tombstone")
    data = b"".join(store.get(h) for h in entry["chunks"])  # KeyError if missing
    if sha(data) != entry["file_hash"]:
        raise ValueError("file_hash mismatch")
    target = Path(root) / (dest or entry["path"])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


class Setup:
    def __init__(self, tmp_path: Path, me: str = A) -> None:
        self.root = tmp_path
        cfg = Config(sync_dir=tmp_path, name="me", secret_file=tmp_path / "secret")
        self.index = Index(tmp_path)
        self.store = ChunkStore(tmp_path)
        self.sent: list[dict] = []
        self.engine = Engine(
            cfg, self.index, self.store, self.sent.append, peer_id=me, assemble=fake_assemble
        )

    def entry(self, path: str, data: bytes, version: dict, *, store: bool = True) -> dict:
        if store:
            self.store.put(sha(data), data)
        return {
            "path": path,
            "size": len(data),
            "file_hash": sha(data),
            "chunks": [sha(data)] if data else [],
            "version": version,
            "deleted": False,
        }

    def local(self, path: str, data: bytes, version: dict) -> dict:
        """A file on disk that the scanner already indexed."""
        e = self.entry(path, data, version)
        (self.root / path).parent.mkdir(parents=True, exist_ok=True)
        (self.root / path).write_bytes(data)
        self.index.put(e)
        return e


def tomb(path: str, version: dict) -> dict:
    return {
        "path": path,
        "size": 0,
        "file_hash": EMPTY_FILE_HASH,
        "chunks": [],
        "version": version,
        "deleted": True,
    }


# ------------------------------------------------------------ conflict names (§9.4)


@pytest.mark.parametrize(
    ("path", "want"),
    [
        ("report.txt", "report.conflict-A1F3C9D2.txt"),
        ("a.tar.gz", "a.tar.conflict-A1F3C9D2.gz"),
        ("Makefile", "Makefile.conflict-A1F3C9D2"),
        (".bashrc", ".bashrc.conflict-A1F3C9D2"),
        ("docs/v1.2/notes.md", "docs/v1.2/notes.conflict-A1F3C9D2.md"),
        ("dir.d/README", "dir.d/README.conflict-A1F3C9D2"),
    ],
)
def test_conflict_name(path: str, want: str) -> None:
    assert conflict_name(path, "A1F3C9D2") == want


def test_resolve_uses_highest_peer_in_either_list() -> None:
    # local ahead on A and C, remote ahead on B: C is highest, so local keeps the name.
    assert resolve({A: 2, B: 1, C: 1}, {A: 1, B: 2, C: 0}) == (False, C, B)
    # swapped roles give the mirrored answer on the other peer
    assert resolve({A: 1, B: 2, C: 0}, {A: 2, B: 1, C: 1}) == (True, C, B)


def test_resolve_same_answer_on_every_peer() -> None:
    x, y = {A: 3, B: 1}, {A: 1, C: 1}
    keeps_y, winner, tag = resolve(x, y)
    keeps_x, winner2, tag2 = resolve(y, x)
    assert keeps_y != keeps_x and winner == winner2 == C and tag == tag2 == B


def test_resolve_rejects_non_concurrent() -> None:
    with pytest.raises(ValueError):
        resolve({A: 2}, {A: 1})


# ------------------------------------------------------------ on_remote_entry (§9.3)


def test_no_local_entry_fetches(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    r = s.entry("a.txt", b"hi", {B: 1}, store=False)
    assert s.engine.on_remote_entry(B, r) == Action("fetch", r)


def test_remote_dominates_fetch(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    s.local("a.txt", b"old", {A: 1})
    r = s.entry("a.txt", b"new", {A: 1, B: 1}, store=False)
    assert s.engine.on_remote_entry(B, r).kind == "fetch"


def test_local_dominates_skip(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    s.local("a.txt", b"new", {A: 2})
    r = s.entry("a.txt", b"old", {A: 1}, store=False)
    assert s.engine.on_remote_entry(B, r).kind == "skip"


def test_equal_skip(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    e = s.local("a.txt", b"x", {A: 1})
    assert s.engine.on_remote_entry(B, dict(e)).kind == "skip"


def test_concurrent_conflict(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    s.local("a.txt", b"mine", {A: 1})
    r = s.entry("a.txt", b"theirs", {B: 1}, store=False)
    assert s.engine.on_remote_entry(B, r).kind == "conflict"


def test_concurrent_same_hash_merges(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    s.local("a.txt", b"same", {A: 1})
    r = s.entry("a.txt", b"same", {B: 1})
    assert s.engine.on_remote_entry(B, r).kind == "skip"
    assert s.index.get("a.txt")["version"] == {A: 1, B: 1}
    assert sorted(p.name for p in tmp_path.iterdir() if p.is_file()) == ["a.txt"]


def test_tombstone_gives_delete(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    s.local("a.txt", b"x", {A: 1})
    t = tomb("a.txt", {A: 1, B: 1})
    assert s.engine.on_remote_entry(B, t) == Action("delete", t)


# ------------------------------------------------------------ on_fetched (§7.3)


def test_on_fetched_writes_file_and_index(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    r = s.entry("sub/a.txt", b"content", {B: 1})
    s.engine.on_fetched(r)
    assert (tmp_path / "sub/a.txt").read_bytes() == b"content"
    assert s.index.get("sub/a.txt") == r
    assert s.sent == []  # a plain fetch is not re-announced


def test_on_fetched_missing_chunk_leaves_index(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    old = s.local("a.txt", b"old", {A: 1})
    r = s.entry("a.txt", b"new", {A: 1, B: 1}, store=False)
    s.engine.on_fetched(r)
    assert s.index.get("a.txt") == old
    assert (tmp_path / "a.txt").read_bytes() == b"old"


def test_on_fetched_keeps_unscanned_local_edit(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    old = s.local("a.txt", b"old", {A: 1})
    (tmp_path / "a.txt").write_bytes(b"edited, not scanned yet")
    s.engine.on_fetched(s.entry("a.txt", b"new", {A: 1, B: 1}))
    assert (tmp_path / "a.txt").read_bytes() == b"edited, not scanned yet"
    assert s.index.get("a.txt") == old


def test_on_fetched_dropped_if_newer_entry_arrived(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    s.local("a.txt", b"newest", {A: 1, B: 2})
    s.engine.on_fetched(s.entry("a.txt", b"stale", {A: 1, B: 1}))
    assert (tmp_path / "a.txt").read_bytes() == b"newest"


# ------------------------------------------------------------ tombstones (§9.5)


def test_delete_removes_file(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    s.local("a.txt", b"x", {A: 1})
    t = tomb("a.txt", {A: 1, B: 1})
    s.engine.apply(Action("delete", t))
    assert not (tmp_path / "a.txt").exists()
    assert s.index.get("a.txt") == t


def test_delete_keeps_locally_edited_file(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    s.local("a.txt", b"x", {A: 1})
    (tmp_path / "a.txt").write_bytes(b"edited")
    t = tomb("a.txt", {A: 1, B: 1})
    s.engine.apply(Action("delete", t))
    assert (tmp_path / "a.txt").read_bytes() == b"edited"
    # tombstone recorded; the scanner's next edit entry will dominate it
    assert s.index.get("a.txt") == t


def test_delete_without_local_entry(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    t = tomb("gone.txt", {B: 1})
    s.engine.apply(Action("delete", t))
    assert s.index.get("gone.txt") == t


# ------------------------------------------------------------ conflicts (§9.4)


def test_conflict_local_keeps_name_and_resolves(tmp_path: Path) -> None:
    s = Setup(tmp_path, me=B)  # B > A: our version keeps the name, we resolve
    s.local("report.txt", b"mine", {B: 1})
    r = s.entry("report.txt", b"theirs", {A: 1})
    s.engine.apply(Action("conflict", r))
    assert (tmp_path / "report.txt").read_bytes() == b"mine"
    assert (tmp_path / f"report.conflict-{A}.txt").read_bytes() == b"theirs"
    got = s.index.get("report.txt")
    assert got["file_hash"] == sha(b"mine")
    assert got["version"] == {A: 1, B: 2}  # merged, then own bump
    assert s.sent == [got]


def test_conflict_remote_keeps_name_loser_no_bump(tmp_path: Path) -> None:
    s = Setup(tmp_path, me=A)  # remote B wins; we only write copies
    s.local("report.txt", b"mine", {A: 1})
    r = s.entry("report.txt", b"theirs", {B: 1})
    s.engine.apply(Action("conflict", r))
    assert (tmp_path / "report.txt").read_bytes() == b"theirs"
    assert (tmp_path / f"report.conflict-{A}.txt").read_bytes() == b"mine"
    got = s.index.get("report.txt")
    assert got["file_hash"] == sha(b"theirs")
    assert got["version"] == {A: 1, B: 1}  # merged, no bump
    assert s.sent == []


def test_both_peers_write_same_files(tmp_path: Path) -> None:
    pa, pb = tmp_path / "a", tmp_path / "b"
    pa.mkdir()
    pb.mkdir()
    sa, sb = Setup(pa, me=A), Setup(pb, me=B)
    ea = sa.local("f.txt", b"from A", {A: 1})
    eb = sb.local("f.txt", b"from B", {B: 1})
    sa.entry("f.txt", b"from B", {B: 1})
    sb.entry("f.txt", b"from A", {A: 1})
    sa.engine.apply(Action("conflict", eb))
    sb.engine.apply(Action("conflict", ea))
    for root in (pa, pb):
        assert (root / "f.txt").read_bytes() == b"from B"
        assert (root / f"f.conflict-{A}.txt").read_bytes() == b"from A"
    # the resolver's NOTIFY dominates the loser's merged vector: no loop
    assert sb.sent and sb.sent[0]["version"] == {A: 1, B: 2}
    assert sa.engine.on_remote_entry(B, sb.sent[0]).kind == "fetch"


def test_three_peers_winner_not_involved(tmp_path: Path) -> None:
    # local ahead on C, remote ahead on A; we are B: C's side keeps the name, nobody here bumps.
    s = Setup(tmp_path, me=B)
    s.local("x.md", b"local", {C: 1})
    r = s.entry("x.md", b"remote", {A: 1})
    s.engine.apply(Action("conflict", r))
    assert (tmp_path / "x.md").read_bytes() == b"local"
    assert (tmp_path / f"x.conflict-{A}.md").read_bytes() == b"remote"
    assert s.index.get("x.md")["version"] == {A: 1, C: 1}
    assert s.sent == []


def test_conflict_with_unscanned_edit_waits(tmp_path: Path) -> None:
    s = Setup(tmp_path, me=A)
    old = s.local("a.txt", b"mine", {A: 1})
    (tmp_path / "a.txt").write_bytes(b"edited again")
    s.engine.apply(Action("conflict", s.entry("a.txt", b"theirs", {B: 1})))
    assert (tmp_path / "a.txt").read_bytes() == b"edited again"
    assert s.index.get("a.txt") == old
    assert not (tmp_path / f"a.conflict-{A}.txt").exists()


def test_conflict_missing_chunk_changes_nothing(tmp_path: Path) -> None:
    s = Setup(tmp_path, me=B)
    old = s.local("a.txt", b"mine", {B: 1})
    s.engine.apply(Action("conflict", s.entry("a.txt", b"theirs", {A: 1}, store=False)))
    assert s.index.get("a.txt") == old
    assert s.sent == []


def test_remote_tombstone_wins_conflict_keeps_copy(tmp_path: Path) -> None:
    s = Setup(tmp_path, me=A)
    s.local("a.txt", b"mine", {A: 1})
    s.engine.apply(Action("conflict", tomb("a.txt", {B: 1})))
    assert not (tmp_path / "a.txt").exists()
    assert (tmp_path / f"a.conflict-{A}.txt").read_bytes() == b"mine"
    assert s.index.get("a.txt")["deleted"] is True


def test_on_change_called_outside_lock(tmp_path: Path) -> None:
    s = Setup(tmp_path, me=B)
    held: list[bool] = []

    def cb(entry: dict) -> None:
        # The lock must be free: a callback that takes it would otherwise deadlock.
        ok = s.engine._lock.acquire(blocking=False)
        held.append(not ok)
        if ok:
            s.engine._lock.release()

    s.engine.on_change = cb
    s.local("a.txt", b"mine", {B: 1})
    s.engine.apply(Action("conflict", s.entry("a.txt", b"theirs", {A: 1})))
    assert held == [False]


def test_concurrent_calls_are_safe(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    entries = [s.entry(f"f{i}.txt", bytes([i]), {B: 1}) for i in range(20)]
    threads = [threading.Thread(target=s.engine.on_fetched, args=(e,)) for e in entries]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(s.index.entries()) == 20
