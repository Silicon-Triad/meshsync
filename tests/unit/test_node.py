"""Tests for node.py (§6.4, §6.7, §8.3). Localhost sockets, static peers, small timers."""

import json
import logging
import re
import socket
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from fakes import (
    FakeEngine,
    FakeIndex,
    FakeScanner,
    FakeScheduler,
    FakeStore,
    make_entry,
    sha,
)
from meshsync import __main__ as cli
from meshsync import node as node_mod
from meshsync import status as status_mod
from meshsync.net import manager
from meshsync.net.auth import PeerIdentity
from meshsync.net.connection import Connection, RemoteError, State
from meshsync.net.manager import PeerManager
from meshsync.node import Node, load_or_create_peer_id
from meshsync.protocol.constants import ErrorCode, MsgType
from meshsync.protocol.framing import send_msg
from meshsync.status import StatusWriter

KEY = bytes(range(32))
ALICE = PeerIdentity("A1F3C9D2", "alice")  # lower id: dials
BOB = PeerIdentity("B77C0E11", "bob")
WAIT = 3.0


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_until(cond: Callable[[], bool], timeout: float = WAIT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


class NoDiscovery:
    def __init__(self, on_event: Callable) -> None:
        pass

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass


class Side:
    """One Node with fakes, plus the fakes themselves for assertions."""

    def __init__(self, me: PeerIdentity, *, force: str | None = None, delay: float = 0.0,
                 static: list[tuple[str, int]] | None = None) -> None:  # fmt: skip
        self.port = free_port()
        self.index = FakeIndex()
        self.store = FakeStore()
        self.engine = FakeEngine(self.index, force)
        self.scanner = FakeScanner()
        mgr = PeerManager(
            KEY, me, tcp_port=self.port, udp_port=0, static_peers=static,
            discovery_factory=NoDiscovery, index_rev=lambda: self.index.rev,
        )  # fmt: skip
        self.scheduler = FakeScheduler(self.store, delay, time.sleep)
        self.node = Node(
            None,  # type: ignore[arg-type]  # config is only used when no manager is given
            index=self.index, store=self.store, engine=self.engine,
            scheduler=self.scheduler,
            scanner=self.scanner, manager=mgr,
        )  # fmt: skip
        self.mgr = mgr

    def add_file(self, path: str, chunks: list[bytes], version: dict[str, int]) -> dict:
        entry = make_entry(path, chunks, version)
        for c in chunks:
            self.store.put(sha(c), c)
        self.index.put(entry)
        return entry


@pytest.fixture
def sides(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[Side]]:
    monkeypatch.setattr(manager, "ANNOUNCE_INTERVAL", 0.1)
    monkeypatch.setattr(manager, "PING_INTERVAL", 0.1)
    made: list[Side] = []
    yield made
    for s in made:
        s.node.stop()


def pair(sides: list[Side], **kw_b: object) -> tuple[Side, Side]:
    """alice dials bob. Both nodes are started."""
    b = Side(BOB, **kw_b)  # type: ignore[arg-type]
    a = Side(ALICE, static=[("127.0.0.1", b.port)])
    sides += [a, b]
    b.node.start()
    a.node.start()
    return a, b


def test_peer_id_created_once_and_reused(tmp_path: Path) -> None:
    first = load_or_create_peer_id(tmp_path)
    assert re.fullmatch(r"[0-9A-F]{8}", first)
    assert (tmp_path / ".meshsync" / "peer_id").read_text().strip() == first
    assert load_or_create_peer_id(tmp_path) == first


def test_index_exchange_fetches_missing_file(sides: list[Side]) -> None:
    a, b = pair(sides)
    # Added before start() would be cleaner, but peers connect lazily, so this is still the
    # INDEX path as long as the file is in the index before B asks.
    entry = a.add_file("docs/x.txt", [b"hello", b"world"], {ALICE.peer_id: 1})
    assert wait_until(lambda: b.index.get("docs/x.txt") == entry)
    assert all(b.store.has(h) for h in entry["chunks"])
    assert b.engine.fetched == [entry]
    assert wait_until(
        lambda: all(p.conn.state is State.SYNCED for s in (a, b) for p in s.mgr.peers())
    )


def test_notify_from_scanner_reaches_peer(sides: list[Side]) -> None:
    a, b = pair(sides)
    assert wait_until(lambda: a.mgr.peers() and b.mgr.peers())
    entry = a.add_file("n.txt", [b"abc"], {ALICE.peer_id: 1})
    a.scanner.trigger(entry)
    assert wait_until(lambda: b.index.get("n.txt") == entry)
    assert b.store.has(entry["chunks"][0])


def test_chunk_req_missing_hash_is_nonfatal(sides: list[Side]) -> None:
    a, b = pair(sides)
    assert wait_until(lambda: a.mgr.peers() and b.mgr.peers())
    conn = a.mgr.peers()[0].conn
    with pytest.raises(RemoteError) as exc:
        conn.request(MsgType.CHUNK_REQ, {"hash": "0" * 64}).result(WAIT)
    assert exc.value.args and "CHUNK_NOT_FOUND" in str(exc.value)
    assert ErrorCode.CHUNK_NOT_FOUND.name in str(exc.value)
    assert not conn.closed
    assert conn.request(MsgType.INDEX_REQ, {}).result(WAIT)[0] == MsgType.INDEX


def test_conflict_fetches_chunks_then_applies(sides: list[Side]) -> None:
    # #49, §7.2, PROTOCOL.md §9.4: remote chunks must be in the store before apply("conflict")
    a, b = pair(sides, force="conflict")
    seen_at_apply: list[tuple[int, bool]] = []
    real_apply = b.engine.apply

    def apply(action: object) -> None:
        seen_at_apply.append((len(b.scheduler.calls), b.store.has(sha(b"x"))))
        real_apply(action)  # type: ignore[arg-type]

    b.engine.apply = apply  # type: ignore[method-assign]
    entry = a.add_file("c.txt", [b"x"], {ALICE.peer_id: 1})
    assert wait_until(lambda: [x.kind for x in b.engine.applied] == ["conflict"])
    assert b.engine.applied[0].entry == entry
    assert b.scheduler.calls == [entry]
    assert seen_at_apply == [(1, True)]
    assert b.engine.fetched == []


def test_failed_conflict_fetch_skips_apply_and_worker_survives(sides: list[Side]) -> None:
    a, b = pair(sides, force="conflict")
    b.scheduler.fail_paths.add("bad.txt")
    bad = a.add_file("bad.txt", [b"x"], {ALICE.peer_id: 1})
    a.scanner.trigger(bad)
    assert wait_until(lambda: bad in b.scheduler.calls)
    time.sleep(0.2)
    assert b.engine.applied == []
    b.engine.force = None  # next entry is a plain "fetch"
    good = a.add_file("good.txt", [b"y"], {ALICE.peer_id: 1})
    a.scanner.trigger(good)
    assert wait_until(lambda: b.index.get("good.txt") == good)
    assert b.engine.applied == []


def test_delete_applies_without_scheduler(sides: list[Side]) -> None:
    a, b = pair(sides, force="delete")
    a.add_file("d.txt", [b"x"], {ALICE.peer_id: 1})
    assert wait_until(lambda: [x.kind for x in b.engine.applied] == ["delete"])
    time.sleep(0.2)
    assert b.scheduler.calls == []


def test_skip_does_nothing(sides: list[Side]) -> None:
    a, b = pair(sides, force="skip")
    a.add_file("s.txt", [b"x"], {ALICE.peer_id: 1})
    assert wait_until(
        lambda: all(p.conn.state is State.SYNCED for p in b.mgr.peers()) and b.mgr.peers()
    )
    time.sleep(0.2)
    assert b.engine.applied == [] and b.engine.fetched == []
    assert b.index.get("s.txt") is None


def test_slow_fetch_does_not_block_reader(sides: list[Side]) -> None:
    a, b = pair(sides, delay=1.0)
    entry = a.add_file("slow.txt", [b"x"], {ALICE.peer_id: 1})
    a.scanner.trigger(entry)
    assert wait_until(lambda: b.mgr.peers())
    time.sleep(0.2)  # B's fetch is now sleeping in its worker thread
    t0 = time.monotonic()
    conn = b.mgr.peers()[0].conn
    conn.request(MsgType.INDEX_REQ, {}).result(WAIT)  # answered by A, but B's reader must be free
    a_conn = a.mgr.peers()[0].conn
    a_conn.request(MsgType.INDEX_REQ, {}).result(WAIT)  # answered by B's reader thread
    assert time.monotonic() - t0 < 0.8
    assert wait_until(lambda: b.index.get("slow.txt") == entry)


def test_run_with_unimplemented_modules_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Force the "missing module" path: once the engine (#14) and scheduler (#15) both exist,
    # the real build_node succeeds and `run` would block forever.
    def missing(config: object) -> None:
        raise node_mod.ComponentsMissing("engine")

    monkeypatch.setattr(cli, "build_node", missing)
    sync = tmp_path / "sync"
    sync.mkdir()
    key = tmp_path / "key"
    key.write_text("00" * 32 + "\n")
    key.chmod(0o600)
    rc = cli.main(
        ["run", "--dir", str(sync), "--name", "t", "--secret-file", str(key), "--plain"]
    )  # fmt: skip
    assert rc == 1
    assert "sync/transfer modules not implemented yet" in capsys.readouterr().err


def test_handle_remote_after_stop_does_not_raise(sides: list[Side]) -> None:
    a, _ = pair(sides)
    a.node.stop()
    entry = make_entry("late.txt", [b"x"], {BOB.peer_id: 1})
    a.node._handle_remote(BOB.peer_id, entry)  # must not raise RuntimeError
    assert a.index.get("late.txt") is None


def test_index_timeout_still_marks_synced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(node_mod, "HANDSHAKE_TIMEOUT", 0.1)
    side = Side(BOB)
    sa, sb = socket.socketpair()
    sa.settimeout(WAIT)
    conn = Connection(sb, ALICE, False)
    conn.start()  # the scripted peer on `sa` never answers INDEX_REQ
    try:
        peer = SimpleNamespace(peer_id=ALICE.peer_id, conn=conn)
        side.node._exchange_index(peer)  # type: ignore[arg-type]
        assert conn.state is State.SYNCED
    finally:
        conn.close("test")
        sa.close()


def test_malformed_notify_closes_link_and_never_reaches_engine() -> None:
    side = Side(BOB)
    sa, sb = socket.socketpair()
    sa.settimeout(WAIT)
    conn = Connection(sb, ALICE, False)
    conn.on(MsgType.NOTIFY, side.node._on_notify)
    conn.start()
    try:
        send_msg(sa, MsgType.NOTIFY, 1, b"{}")  # no "entry": decode_body rejects it (BAD_HEADER)
        assert wait_until(lambda: conn.closed)
        assert side.engine.applied == [] and side.engine.fetched == []
    finally:
        conn.close("test")
        sa.close()


@pytest.mark.parametrize("body", [{}, None, [], {"entry": None}, {"entry": "x"}])
def test_on_notify_drops_malformed_body(body: object) -> None:
    side = Side(BOB)
    conn = SimpleNamespace(remote=ALICE)
    side.node._on_notify(conn, 1, body)  # type: ignore[arg-type]  # must not raise
    assert side.engine.applied == [] and side.engine.fetched == []
    assert side.index.entries() == []


# ------------------------------------------------------------ status file (#64)


@pytest.fixture
def no_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    # Only event hooks write the file during a test, not the periodic refresh.
    monkeypatch.setattr(status_mod, "SCAN_INTERVAL", 60)


def with_status(side: Side, sync_dir: Path, me: PeerIdentity) -> Path:
    sync_dir.mkdir()
    info = {"name": me.name, "peer_id": me.peer_id, "mode": "plain", "tcp_port": side.port,
            "udp_port": 0}  # fmt: skip
    side.node.status = StatusWriter(sync_dir, info)
    return status_mod.status_path(sync_dir)


def read(path: Path) -> dict:
    return json.loads(path.read_text())


def test_status_file_tracks_peers_and_local_changes(
    sides: list[Side], tmp_path: Path, no_refresh: None
) -> None:
    b = Side(BOB)
    a = Side(ALICE, static=[("127.0.0.1", b.port)])
    sides += [a, b]
    path = with_status(a, tmp_path / "a", ALICE)
    b.node.start()
    a.node.start()
    assert set(read(path)) >= {
        "name", "peer_id", "mode", "tcp_port", "udp_port", "pid", "started_at", "updated_at",
        "index_rev", "entries", "tombstones", "peers", "stopped",
    }  # fmt: skip
    # peer up, then SYNCED after the index exchange
    assert wait_until(lambda: [p["state"] for p in read(path)["peers"]] == ["SYNCED"])
    peer = read(path)["peers"][0]
    assert (peer["name"], peer["peer_id"], peer["tcp_port"]) == ("bob", BOB.peer_id, b.port)
    assert peer["last_seen"] is not None
    # local change
    entry = a.add_file("n.txt", [b"abc"], {ALICE.peer_id: 1})
    a.add_file("gone.txt", [], {ALICE.peer_id: 1})
    a.index.put({**a.index.get("gone.txt"), "deleted": True})  # type: ignore[dict-item]
    a.scanner.trigger(entry)
    data = read(path)
    assert (data["index_rev"], data["entries"], data["tombstones"]) == (a.index.rev, 1, 1)
    # peer down
    b.node.stop()
    assert wait_until(lambda: read(path)["peers"] == [])
    # clean stop keeps the file, marked stopped
    a.node.stop()
    assert read(path)["stopped"] is True
    assert not list(path.parent.glob("*.tmp"))


def test_status_write_failure_is_logged_and_node_runs(
    sides: list[Side], tmp_path: Path, no_refresh: None, caplog: pytest.LogCaptureFixture
) -> None:
    a = Side(ALICE)
    sides.append(a)
    with_status(a, tmp_path / "a", ALICE)

    def boom(data: dict) -> None:
        raise OSError("disk full")

    a.node.status._write = boom  # type: ignore[union-attr,method-assign]
    with caplog.at_level(logging.WARNING, logger="meshsync.status"):
        a.node.start()
        entry = a.add_file("x.txt", [b"x"], {ALICE.peer_id: 1})
        a.scanner.trigger(entry)  # must not raise
    assert "could not write" in caplog.text
    assert a.mgr.peers() == [] and not a.node._stopped
    a.node.stop()  # must not raise either
