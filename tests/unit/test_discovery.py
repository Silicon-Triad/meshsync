"""Tests for net/discovery.py (§6.2, D11, decision (b))."""

import json
import socket
import threading

import pytest

from meshsync.net.discovery import (
    Discovery,
    PeerIdentity,
    PeerInfo,
    PeerTable,
    build_announce,
    make_tag,
    next_ts,
)
from meshsync.protocol.constants import ANNOUNCE_MAX_SKEW, MAX_DATAGRAM, PEER_EXPIRY

KEY = b"group-secret"
ME = PeerIdentity("AAAAAAAA", "ahmed")
OTHER = PeerIdentity("BBBBBBBB", "mujtaba")
NOW = 1_730_473_200
IP = "192.168.1.20"


def announce(ts: int = NOW, type: str = "ANNOUNCE", key: bytes = KEY, rev: int = 1) -> bytes:  # noqa: A002
    return build_announce(key, OTHER, 47001, rev, ts, type)


@pytest.fixture
def table() -> PeerTable:
    return PeerTable(KEY, ME.peer_id)


def test_up_update_down(table: PeerTable) -> None:
    assert table.handle_datagram(announce(NOW), IP, NOW) == "up"
    assert table.handle_datagram(announce(NOW + 5, rev=2), IP, NOW + 5) == "update"
    assert table.peers()[0].index_rev == 2
    assert table.handle_datagram(announce(NOW + 6, "LEAVE"), IP, NOW + 6) == "down"
    assert table.peers() == []


def test_tag_covers_all_fields_but_tag() -> None:
    fields = json.loads(announce())
    assert fields["tag"] == make_tag(KEY, fields)


def test_wrong_key_dropped(table: PeerTable) -> None:
    assert table.handle_datagram(announce(key=b"other"), IP, NOW) is None


def test_bad_tag_dropped(table: PeerTable) -> None:
    fields = json.loads(announce())
    fields["tag"] = "0" * 64
    assert table.handle_datagram(json.dumps(fields).encode(), IP, NOW) is None


def test_tampered_field_dropped(table: PeerTable) -> None:
    fields = json.loads(announce())
    fields["tcp_port"] = 6666  # tag no longer matches
    assert table.handle_datagram(json.dumps(fields).encode(), IP, NOW) is None


@pytest.mark.parametrize("skew", [ANNOUNCE_MAX_SKEW + 1, -(ANNOUNCE_MAX_SKEW + 1)])
def test_stale_or_future_ts_dropped(table: PeerTable, skew: int) -> None:
    assert table.handle_datagram(announce(NOW + skew), IP, NOW) is None


def test_ts_at_skew_limit_accepted(table: PeerTable) -> None:
    assert table.handle_datagram(announce(NOW - ANNOUNCE_MAX_SKEW), IP, NOW) == "up"


def test_replay_dropped(table: PeerTable) -> None:
    data = announce(NOW)
    assert table.handle_datagram(data, IP, NOW) == "up"
    assert table.handle_datagram(data, IP, NOW + 1) is None  # identical datagram
    assert table.handle_datagram(announce(NOW - 1), IP, NOW + 1) is None  # older ts


def test_replay_after_leave_does_not_revive(table: PeerTable) -> None:
    old = announce(NOW)
    table.handle_datagram(old, IP, NOW)
    table.handle_datagram(announce(NOW + 1, "LEAVE"), IP, NOW + 1)
    assert table.handle_datagram(old, IP, NOW + 2) is None


def test_next_ts_strictly_increases_within_one_second() -> None:
    assert next_ts(NOW + 0.2, 0) == NOW
    assert next_ts(NOW + 0.9, NOW) == NOW + 1  # same second: bumped so (b) accepts it
    assert next_ts(NOW + 5.0, NOW + 1) == NOW + 5  # clock ahead again: back to wall clock


def test_leave_in_same_second_as_announce_accepted(table: PeerTable) -> None:
    # Regression: CI dropped a LEAVE sent in the same second as the last ANNOUNCE as a replay.
    first = next_ts(NOW, 0)
    table.handle_datagram(announce(first), IP, NOW)
    leave = announce(next_ts(NOW, first), "LEAVE")
    assert table.handle_datagram(leave, IP, NOW) == "down"


def test_own_peer_id_ignored(table: PeerTable) -> None:
    data = build_announce(KEY, ME, 47001, 0, NOW)
    assert table.handle_datagram(data, IP, NOW) is None


@pytest.mark.parametrize(
    "data", [b"{not json", b"\xff\xfe", b"[]", b'{"magic":"MS"}', b"x" * (MAX_DATAGRAM + 1)]
)
def test_malformed_ignored(table: PeerTable, data: bytes) -> None:
    assert table.handle_datagram(data, IP, NOW) is None


def test_expire(table: PeerTable) -> None:
    table.handle_datagram(announce(NOW), IP, NOW)
    assert table.expire(NOW + PEER_EXPIRY) == []
    gone = table.expire(NOW + PEER_EXPIRY + 1)
    assert [p.peer_id for p in gone] == [OTHER.peer_id]
    assert table.peers() == []


def test_addr_from_source_ip(table: PeerTable) -> None:
    table.handle_datagram(announce(NOW), "10.0.0.7", NOW)
    table.handle_datagram(announce(NOW + 1), "10.0.0.8", NOW + 1)
    assert table.peers()[0].addr == "10.0.0.8"


def test_peers_returns_copies(table: PeerTable) -> None:
    table.handle_datagram(announce(NOW), IP, NOW)
    table.peers()[0].index_rev = 999
    assert table.peers()[0].index_rev == 1


# ---------------------------------------------------------------- one real-socket test


def _free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _loopback_broadcast_works(port: int, dest: str) -> bool:
    with (
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as rx,
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as tx,
    ):
        rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        rx.bind(("", port))
        rx.settimeout(0.5)
        tx.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            tx.sendto(b"probe", (dest, port))
            return rx.recvfrom(16)[0] == b"probe"
        except OSError:
            return False


@pytest.mark.slow
def test_two_peers_see_each_other() -> None:
    port = _free_udp_port()
    dest = "127.255.255.255"
    if not _loopback_broadcast_works(port, dest):
        dest = "127.0.0.1"  # unicast to loopback; both sockets share the port via SO_REUSEADDR
        if not _loopback_broadcast_works(port, dest):
            pytest.skip("UDP to loopback unavailable here")

    events: list[tuple[str, str, str]] = []  # (receiver, event, peer_id)
    lock = threading.Lock()
    both_up = threading.Event()
    b_down = threading.Event()

    def recorder(who: str):
        def on_event(event: str, peer: PeerInfo) -> None:
            with lock:
                events.append((who, event, peer.peer_id))
                ups = {(w, p) for w, e, p in events if e == "up"}
                if ("a", OTHER.peer_id) in ups and ("b", ME.peer_id) in ups:
                    both_up.set()
                if ("a", "down", OTHER.peer_id) in events:
                    b_down.set()

        return on_event

    a = Discovery(KEY, ME, 47001, lambda: 0, recorder("a"), port, dest)
    b = Discovery(KEY, OTHER, 47002, lambda: 0, recorder("b"), port, dest)
    a.start()
    b.start()
    try:
        # Both send their first ANNOUNCE immediately, but whether unicast to a shared port reaches
        # every socket is OS-dependent; skip rather than fail if it does not.
        if not both_up.wait(3):
            pytest.skip(f"peers did not see each other on {dest}: {events}")
        b.stop()
        assert b_down.wait(3), events
    finally:
        a.stop()
        if not b._stop.is_set():
            b.stop()
