"""Tests for net/manager.py (§6.4, §6.7, §8.3, §9.1). Localhost sockets, no UDP, small timers."""

import socket
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from meshsync.net import discovery, manager, tls
from meshsync.net.auth import PeerIdentity
from meshsync.net.connection import establish
from meshsync.net.manager import PeerInfo, PeerManager
from meshsync.protocol.constants import MsgType

KEY = bytes(range(32))
ALICE = PeerIdentity("A1F3C9D2", "alice")  # lowest peer_id: dials the others
BOB = PeerIdentity("B77C0E11", "bob")
CARL = PeerIdentity("C0FFEE00", "carl")
WAIT = 2.0  # every wait in this file is bounded by this many seconds


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


class FakeDiscovery:
    """Stands in for Discovery: no UDP. Tests call .on_event(...) themselves."""

    def __init__(self, on_event: Callable) -> None:
        self.on_event = on_event

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass


class Node:
    """A PeerManager plus a record of its events."""

    def __init__(self, me: PeerIdentity, **kw: object) -> None:
        self.me = me
        self.port = kw.pop("port", None) or free_port()
        self.fake: FakeDiscovery | None = None
        self.ups: list[PeerInfo] = []
        self.downs: list[tuple[PeerInfo, str]] = []
        self.mgr = PeerManager(
            kw.pop("key", KEY),  # type: ignore[arg-type]
            me,
            tcp_port=self.port,
            udp_port=0,
            discovery_factory=self._make_discovery,
            **kw,  # type: ignore[arg-type]
        )
        self.mgr.on("peer_up", self.ups.append)
        self.mgr.on("peer_down", lambda p, reason: self.downs.append((p, reason)))

    def _make_discovery(self, on_event: Callable) -> FakeDiscovery:
        self.fake = FakeDiscovery(on_event)
        return self.fake

    def link_to(self, other: "Node") -> tuple[str, int]:
        return ("127.0.0.1", other.port)

    def ids(self) -> set[str]:
        return {p.peer_id for p in self.mgr.peers()}


@pytest.fixture
def nodes(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[Node]]:
    monkeypatch.setattr(manager, "ANNOUNCE_INTERVAL", 0.1)
    monkeypatch.setattr(manager, "PING_INTERVAL", 0.1)
    made: list[Node] = []
    yield made
    for n in made:
        n.mgr.stop()


def node(nodes: list[Node], me: PeerIdentity, **kw: object) -> Node:
    n = Node(me, **kw)
    nodes.append(n)
    return n


def test_two_peers_connect(nodes: list[Node]) -> None:
    b = node(nodes, BOB)
    a = node(nodes, ALICE, static_peers=[("127.0.0.1", b.port)])
    b.mgr.start()
    a.mgr.start()
    assert wait_until(lambda: len(a.ups) == 1 and len(b.ups) == 1)
    assert a.ids() == {BOB.peer_id}
    assert b.ids() == {ALICE.peer_id}
    assert a.mgr.get(BOB.peer_id).name == "bob"  # type: ignore[union-attr]
    assert a.mgr.get("00000000") is None


def test_full_mesh_of_three(nodes: list[Node]) -> None:
    a, b, c = (node(nodes, me) for me in (ALICE, BOB, CARL))
    for n in (a, b, c):  # everyone dials everyone: the duplicate rule must sort it out
        n.mgr._static_peers = [n.link_to(o) for o in (a, b, c) if o is not n]
        n.mgr.start()
    assert wait_until(lambda: all(len(n.ids()) == 2 for n in (a, b, c)))
    time.sleep(0.3)  # no flapping afterwards
    assert all(len(n.ids()) == 2 for n in (a, b, c))


def test_dial_rule_only_lower_peer_id_dials(nodes: list[Node]) -> None:
    b = node(nodes, BOB)
    dialed: list[object] = []
    b.mgr._start_dialer = dialed.append  # type: ignore[method-assign]
    row = discovery.PeerInfo(CARL.peer_id, "carl", "127.0.0.1", 1, 0, 0.0, 0)
    b.mgr._on_discovery("up", row)  # bob < carl: bob dials
    assert dialed == [CARL.peer_id]
    row = discovery.PeerInfo(ALICE.peer_id, "alice", "127.0.0.1", 1, 0, 0.0, 0)
    b.mgr._on_discovery("up", row)  # alice < bob: bob waits to be dialled
    b.mgr._on_discovery("update", row)
    assert dialed == [CARL.peer_id]


def test_discovered_peer_is_dialled_and_redialled(nodes: list[Node]) -> None:
    b = node(nodes, BOB)
    a = node(nodes, ALICE)
    b.mgr.start()
    a.mgr.start()
    assert a.fake is not None
    a.fake.on_event("up", discovery.PeerInfo(BOB.peer_id, "bob", "127.0.0.1", b.port, 0, 0.0, 0))
    assert wait_until(lambda: len(a.ups) == 1 and len(b.ups) == 1)
    b.mgr.get(ALICE.peer_id).conn.close("test")  # type: ignore[union-attr]
    assert wait_until(lambda: len(a.downs) == 1)
    assert wait_until(lambda: len(a.ups) == 2)  # still in the UDP table: reconnects
    a.fake.on_event("down", discovery.PeerInfo(BOB.peer_id, "bob", "127.0.0.1", b.port, 0, 0.0, 0))
    assert len(a.mgr.peers()) == 1  # UDP "down" does not kill a working connection


def test_simultaneous_dial_keeps_one_connection(nodes: list[Node]) -> None:
    a, b = node(nodes, ALICE), node(nodes, BOB)
    a.mgr._static_peers = [a.link_to(b)]
    b.mgr._static_peers = [b.link_to(a)]
    a.mgr.start()
    b.mgr.start()
    assert wait_until(lambda: len(a.ids()) == 1 and len(b.ids()) == 1)
    time.sleep(0.3)
    # Both sides kept the link alice (lower id) initiated.
    assert a.mgr.get(BOB.peer_id).we_dialed is True  # type: ignore[union-attr]
    assert b.mgr.get(ALICE.peer_id).we_dialed is False  # type: ignore[union-attr]
    assert len(a.ids()) == len(b.ids()) == 1


def test_wrong_key_is_refused_and_listener_survives(nodes: list[Node]) -> None:
    b = node(nodes, BOB)
    bad = node(nodes, ALICE, key=bytes(32), static_peers=[("127.0.0.1", b.port)])
    b.mgr.start()
    bad.mgr.start()
    time.sleep(0.5)
    assert not bad.ups and not b.ups
    assert bad.mgr.peers() == [] and b.mgr.peers() == []
    good = node(nodes, CARL, static_peers=[("127.0.0.1", b.port)])
    good.mgr.start()
    assert wait_until(lambda: len(b.ups) == 1)  # bob still accepts


def test_tls_mode(nodes: list[Node], certs: Path) -> None:
    def ctx(name: str) -> dict[str, object]:
        return {
            "tls_ctx_server": tls.server_context(certs, name),
            "tls_ctx_client": tls.client_context(certs, name),
        }

    bob_id = PeerIdentity(BOB.peer_id, "bob")
    b = node(nodes, bob_id, **ctx("bob"))
    a = node(nodes, ALICE, static_peers=[("127.0.0.1", b.port)], **ctx("alice"))
    b.mgr.start()
    a.mgr.start()
    assert wait_until(lambda: len(a.ups) == 1 and len(b.ups) == 1)


def test_silent_peer_is_dropped_by_liveness(nodes: list[Node]) -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(WAIT)
    a = node(nodes, ALICE, static_peers=[("127.0.0.1", listener.getsockname()[1])])
    a.mgr.start()
    sock, _ = listener.accept()
    # Authenticated, but nobody ever reads from it, so no PING is answered.
    silent = establish(sock, KEY, BOB, initiator=False, tls_ctx=None)
    try:
        assert wait_until(lambda: len(a.ups) == 1)
        assert wait_until(lambda: len(a.downs) >= 1)
        assert a.downs[0][1] == "liveness"
    finally:
        silent.close(send_bye=False)
        listener.close()


def test_answered_pings_record_rtt(nodes: list[Node]) -> None:
    b = node(nodes, BOB)
    a = node(nodes, ALICE, static_peers=[("127.0.0.1", b.port)])
    b.mgr.start()
    a.mgr.start()
    assert wait_until(lambda: any(p.rtt_ms is not None for p in a.mgr.peers()))
    assert a.mgr.peers()[0].missed_pongs == 0
    assert not a.downs


def test_peer_stop_gives_bye_and_restart_reconnects(nodes: list[Node]) -> None:
    b = node(nodes, BOB)
    a = node(nodes, ALICE, static_peers=[("127.0.0.1", b.port)])
    b.mgr.start()
    a.mgr.start()
    assert wait_until(lambda: len(a.ups) == 1)
    b.mgr.stop()
    assert wait_until(lambda: len(a.downs) == 1)
    assert "BYE" in a.downs[0][1]
    b2 = node(nodes, BOB, port=b.port)  # the same peer comes back
    b2.mgr.start()
    assert wait_until(lambda: len(a.ups) == 2 and len(b2.ups) == 1)


def test_stop_twice_is_harmless(nodes: list[Node]) -> None:
    a = node(nodes, ALICE)
    a.mgr.start()
    a.mgr.stop()
    a.mgr.stop()


def test_handlers_apply_to_current_and_future_connections(nodes: list[Node]) -> None:
    seen: list[str] = []

    def handler(conn: object, req_id: int, body: object) -> None:
        seen.append(conn.remote.name)  # type: ignore[attr-defined]
        conn.reply(req_id, MsgType.INDEX, {"index_rev": 0, "files": []})  # type: ignore[attr-defined]

    b = node(nodes, BOB)
    a = node(nodes, ALICE, static_peers=[("127.0.0.1", b.port)])
    b.mgr.register_handler(MsgType.INDEX_REQ, handler)  # before any connection exists
    b.mgr.start()
    a.mgr.start()
    assert wait_until(lambda: len(a.ups) == 1)
    reply = a.mgr.peers()[0].conn.request(MsgType.INDEX_REQ, {}).result(WAIT)
    assert reply[0] == MsgType.INDEX
    assert seen == ["alice"]

    c = node(nodes, CARL)  # registered after a connection is already up
    c.mgr.start()
    a.mgr._static_peers = []
    a.mgr._start_dialer(("127.0.0.1", c.port))
    assert wait_until(lambda: len(c.ups) == 1)
    c.mgr.register_handler(MsgType.INDEX_REQ, handler)
    reply = a.mgr.get(CARL.peer_id).conn.request(MsgType.INDEX_REQ, {}).result(WAIT)  # type: ignore[union-attr]
    assert reply[0] == MsgType.INDEX


def test_dialing_ourselves_is_refused(nodes: list[Node]) -> None:
    a = node(nodes, ALICE)
    a.mgr._static_peers = [a.link_to(a)]
    a.mgr.start()
    time.sleep(0.5)
    assert a.mgr.peers() == [] and not a.ups


def test_unknown_event_is_rejected(nodes: list[Node]) -> None:
    with pytest.raises(ValueError):
        node(nodes, ALICE).mgr.on("peer_sideways", print)


def test_events_fire_outside_the_lock(nodes: list[Node]) -> None:
    b = node(nodes, BOB)
    a = node(nodes, ALICE, static_peers=[("127.0.0.1", b.port)])
    ok = threading.Event()

    def listener(peer: PeerInfo) -> None:
        a.mgr.peers()  # would deadlock if _fire held the table lock
        ok.set()

    a.mgr.on("peer_up", listener)
    b.mgr.start()
    a.mgr.start()
    assert ok.wait(WAIT)
