"""Tests for transfer/scheduler.py (#15, D12, §7.3, PROTOCOL.md §9.8). No network: fake conns."""

import hashlib
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from pathlib import Path

import pytest

from meshsync.net.connection import RemoteError
from meshsync.net.manager import PeerInfo
from meshsync.protocol.constants import MAX_OUTSTANDING_PER_PEER, ErrorCode, MsgType
from meshsync.sync.store import ChunkStore
from meshsync.transfer.scheduler import Scheduler

WAIT = 5.0


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_entry(parts: list[bytes], path: str = "f.bin") -> dict:
    content = b"".join(parts)
    return {
        "path": path,
        "size": len(content),
        "file_hash": sha(content),
        "chunks": [sha(p) for p in parts],
        "version": {"A1F3C9D2": 1},
        "deleted": False,
    }


def wait_until(cond: Callable[[], bool], timeout: float = WAIT) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return cond()


class FakeConn:
    """Answers CHUNK_REQ from `chunks`. auto=False keeps futures in `pending` for the test."""

    def __init__(self, chunks: dict[str, bytes], *, auto: bool = True) -> None:
        self.chunks = chunks
        self.auto = auto
        self.closed = False
        self.corrupt: set[str] = set()  # reply garbage for these hashes
        self.requested: list[str] = []
        self.pending: list[tuple[str, Future]] = []
        self.max_pending = 0
        self._lock = threading.Lock()

    def request(self, msg_type: MsgType, body: dict) -> Future:
        assert msg_type == MsgType.CHUNK_REQ
        if self.closed:
            raise ConnectionError("connection closed")
        h = body["hash"]
        fut: Future = Future()
        with self._lock:
            self.requested.append(h)
            if not self.auto:
                self.pending.append((h, fut))
                self.max_pending = max(self.max_pending, len(self.pending))
                return fut
        self._answer(h, fut)
        return fut

    def _answer(self, h: str, fut: Future) -> None:
        if h in self.corrupt:
            fut.set_result((MsgType.CHUNK_DATA, b"garbage"))
        elif h in self.chunks:
            fut.set_result((MsgType.CHUNK_DATA, self.chunks[h]))
        else:
            fut.set_exception(RemoteError(ErrorCode.CHUNK_NOT_FOUND, "chunk not found"))

    def answer(self, n: int = 1) -> list[str]:
        """Answer the n oldest pending requests."""
        with self._lock:
            batch, self.pending = self.pending[:n], self.pending[n:]
        for h, fut in batch:
            self._answer(h, fut)
        return [h for h, _ in batch]

    def kill(self) -> None:
        with self._lock:
            self.closed = True
            batch, self.pending = self.pending, []
        for _, fut in batch:
            fut.set_exception(ConnectionError("peer gone"))


def peer(pid: str, conn: FakeConn) -> PeerInfo:
    return PeerInfo(peer_id=pid, name=pid.lower(), addr="127.0.0.1", tcp_port=0, conn=conn)  # type: ignore[arg-type]


def parts(n: int, size: int = 32) -> list[bytes]:
    return [bytes([i % 256]) * size + i.to_bytes(4, "big") for i in range(n)]


def run_bg(fn: Callable[[], None]) -> tuple[threading.Thread, list[BaseException]]:
    errors: list[BaseException] = []

    def target() -> None:
        try:
            fn()
        except BaseException as e:  # noqa: BLE001 - reported to the test
            errors.append(e)

    t = threading.Thread(target=target, daemon=True)
    t.start()
    return t, errors


@pytest.fixture
def store(tmp_path: Path) -> ChunkStore:
    return ChunkStore(tmp_path)


def test_single_peer_fetches_every_chunk(store: ChunkStore) -> None:
    ps = parts(5)
    entry = make_entry(ps)
    conn = FakeConn({sha(p): p for p in ps})
    Scheduler(store).fetch(entry, [peer("AAAAAAAA", conn)])
    assert all(store.get(sha(p)) == p for p in ps)
    assert sorted(conn.requested) == sorted(entry["chunks"])


def test_only_missing_chunks_are_requested(store: ChunkStore) -> None:
    ps = parts(4)
    for p in ps[:3]:
        store.put(sha(p), p)
    conn = FakeConn({sha(p): p for p in ps})
    Scheduler(store).fetch(make_entry(ps), [peer("AAAAAAAA", conn)])
    assert conn.requested == [sha(ps[3])]


def test_nothing_missing_sends_nothing_even_without_peers(store: ChunkStore) -> None:
    ps = parts(2)
    for p in ps:
        store.put(sha(p), p)
    Scheduler(store).fetch(make_entry(ps), [])


def test_repeated_chunk_requested_once(store: ChunkStore) -> None:
    a, b = parts(2)
    conn = FakeConn({sha(a): a, sha(b): b})
    Scheduler(store).fetch(make_entry([a, b, a, a]), [peer("AAAAAAAA", conn)])
    assert sorted(conn.requested) == sorted([sha(a), sha(b)])


def test_load_is_split_round_robin(store: ChunkStore) -> None:
    ps = parts(10)
    chunks = {sha(p): p for p in ps}
    a, b = FakeConn(chunks), FakeConn(chunks)
    Scheduler(store).fetch(make_entry(ps), [peer("AAAAAAAA", a), peer("BBBBBBBB", b)])
    assert len(a.requested) == 5 and len(b.requested) == 5
    assert set(a.requested).isdisjoint(b.requested)


def test_no_peers_raises(store: ChunkStore) -> None:
    with pytest.raises(ConnectionError):
        Scheduler(store).fetch(make_entry(parts(1)), [])


def test_closed_peer_is_skipped(store: ChunkStore) -> None:
    ps = parts(3)
    chunks = {sha(p): p for p in ps}
    dead, live = FakeConn(chunks), FakeConn(chunks)
    dead.closed = True
    Scheduler(store).fetch(make_entry(ps), [peer("AAAAAAAA", dead), peer("BBBBBBBB", live)])
    assert dead.requested == [] and len(live.requested) == 3


def test_peer_closing_before_request_is_skipped(store: ChunkStore) -> None:
    # Open when fetch() starts, closed by the time CHUNK_REQ is sent: request() raises.
    ps = parts(3)
    chunks = {sha(p): p for p in ps}
    gone, live = FakeConn(chunks), FakeConn(chunks)
    gone.request = lambda *a: (_ for _ in ()).throw(ConnectionError("closed"))  # type: ignore[method-assign]
    Scheduler(store).fetch(make_entry(ps), [peer("AAAAAAAA", gone), peer("BBBBBBBB", live)])
    assert all(store.has(sha(p)) for p in ps) and len(live.requested) == 3


def test_not_found_goes_to_another_peer(store: ChunkStore) -> None:
    ps = parts(4)
    full = {sha(p): p for p in ps}
    partial = {sha(p): p for p in ps[:2]}
    a, b = FakeConn(partial), FakeConn(full)
    Scheduler(store).fetch(make_entry(ps), [peer("AAAAAAAA", a), peer("BBBBBBBB", b)])
    assert all(store.has(sha(p)) for p in ps)
    for p in ps[2:]:  # whichever peer was asked first, B delivered it
        assert sha(p) in b.requested


def test_corrupt_chunk_is_rejected_and_refetched(store: ChunkStore) -> None:
    # §10 failure scenario "corrupt chunk": hash mismatch -> re-request elsewhere
    ps = parts(2)
    chunks = {sha(p): p for p in ps}
    bad, good = FakeConn(chunks), FakeConn(chunks)
    bad.corrupt = set(chunks)
    Scheduler(store).fetch(make_entry(ps), [peer("AAAAAAAA", bad), peer("BBBBBBBB", good)])
    assert all(store.get(sha(p)) == p for p in ps)
    assert sorted(good.requested) == sorted(chunks)


def test_no_peer_left_raises_and_keeps_partial_chunks(store: ChunkStore) -> None:
    ps = parts(3)
    conn = FakeConn({sha(p): p for p in ps[:2]})
    with pytest.raises(ConnectionError, match="no peer left"):
        Scheduler(store).fetch(make_entry(ps), [peer("AAAAAAAA", conn)])
    # Resume from any peer later (§5): what arrived stays in the store.
    assert wait_until(lambda: store.has(sha(ps[0])) or store.has(sha(ps[1])))
    other = FakeConn({sha(p): p for p in ps})
    Scheduler(store).fetch(make_entry(ps), [peer("BBBBBBBB", other)])
    assert all(store.has(sha(p)) for p in ps)


def test_window_never_exceeds_limit(store: ChunkStore) -> None:
    ps = parts(30)
    conn = FakeConn({sha(p): p for p in ps}, auto=False)
    t, errors = run_bg(lambda: Scheduler(store).fetch(make_entry(ps), [peer("AAAAAAAA", conn)]))
    while t.is_alive() and len(conn.requested) < len(ps):
        assert wait_until(lambda: len(conn.pending) > 0)
        conn.answer(3)
    while t.is_alive():
        conn.answer(len(conn.pending))
        t.join(0.05)
    assert errors == []
    assert conn.max_pending == MAX_OUTSTANDING_PER_PEER
    # first window is the first chunks in file order (ties broken by file order, D12)
    assert conn.requested[:MAX_OUTSTANDING_PER_PEER] == make_entry(ps)["chunks"][:8]


def test_limit_is_shared_by_concurrent_fetches(store: ChunkStore) -> None:
    ps = parts(24)
    conn = FakeConn({sha(p): p for p in ps}, auto=False)
    sched = Scheduler(store)
    p = peer("AAAAAAAA", conn)
    t1, e1 = run_bg(lambda: sched.fetch(make_entry(ps[:12], "a"), [p]))
    t2, e2 = run_bg(lambda: sched.fetch(make_entry(ps[12:], "b"), [p]))
    assert wait_until(lambda: len(conn.pending) == MAX_OUTSTANDING_PER_PEER)
    time.sleep(0.05)
    assert len(conn.pending) == MAX_OUTSTANDING_PER_PEER
    while t1.is_alive() or t2.is_alive():
        conn.answer(2)
        time.sleep(0.005)
    assert e1 == [] and e2 == []
    assert conn.max_pending == MAX_OUTSTANDING_PER_PEER
    assert all(store.has(sha(x)) for x in ps)


def test_peer_lost_mid_fetch_chunks_reassigned(store: ChunkStore) -> None:
    # §10 failure scenario "peer killed mid-transfer"
    ps = parts(20)
    chunks = {sha(p): p for p in ps}
    a, b = FakeConn(chunks, auto=False), FakeConn(chunks, auto=False)
    pa, pb = peer("AAAAAAAA", a), peer("BBBBBBBB", b)
    t, errors = run_bg(lambda: Scheduler(store).fetch(make_entry(ps), [pa, pb]))
    assert wait_until(lambda: len(a.pending) == 8 and len(b.pending) == 8)
    a.answer(2)
    lost = [h for h, _ in a.pending]
    a.kill()
    while t.is_alive():
        b.answer(len(b.pending))
        t.join(0.02)
    assert errors == []
    assert all(store.has(h) for h in chunks)
    assert set(lost) <= set(b.requested)


def test_rarest_chunk_is_asked_first(store: ChunkStore) -> None:
    # A misses c0, so c0 has one candidate left (B) and goes before the untouched chunks (D12).
    ps = parts(20)
    hs = [sha(p) for p in ps]
    full = {sha(p): p for p in ps}
    a = FakeConn({h: d for h, d in full.items() if h != hs[0]}, auto=False)
    b = FakeConn(full, auto=False)
    t, errors = run_bg(
        lambda: Scheduler(store).fetch(make_entry(ps), [peer("AAAAAAAA", a), peer("BBBBBBBB", b)])
    )
    assert wait_until(lambda: len(a.pending) == 8 and len(b.pending) == 8)
    assert a.requested[0] == hs[0]  # round-robin: c0 -> A, c1 -> B, ...
    a.answer(1)  # CHUNK_NOT_FOUND for c0
    assert wait_until(lambda: len(a.requested) == 9)
    assert a.requested[8] != hs[0]  # A is struck for c0
    b.answer(1)  # frees one B slot
    assert wait_until(lambda: len(b.requested) == 9)
    assert b.requested[8] == hs[0]  # rarest first, ahead of c17..c19
    while t.is_alive():
        a.answer(len(a.pending))
        b.answer(len(b.pending))
        t.join(0.02)
    assert errors == []
    assert all(store.has(h) for h in hs)
