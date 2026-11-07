"""Tests for net/connection.py (§6.4, §6.7, §8.3, §9.1). socketpair + threads, no network."""

import socket
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from pathlib import Path

import pytest

from meshsync.net import connection, tls
from meshsync.net.auth import PeerIdentity
from meshsync.net.connection import Connection, RemoteError, State, establish
from meshsync.protocol.constants import ErrorCode, MsgType
from meshsync.protocol.framing import ProtocolError, recv_msg, send_msg
from meshsync.protocol.messages import decode_body, encode_body

KEY = bytes(range(32))
ALICE = PeerIdentity("A1F3C9D2", "alice")  # lower peer_id: initiator
BOB = PeerIdentity("B77C0E11", "bob")
WAIT = 0.5  # seconds; every wait in this file is bounded by this
HASH = "ab" * 32


def pair(*, a: PeerIdentity = ALICE, b: PeerIdentity = BOB) -> tuple[Connection, Connection]:
    """Two connected, authenticated, NOT started Connections: alice (initiator) and bob."""
    sa, sb = socket.socketpair()
    return Connection(sa, b, True), Connection(sb, a, False)


@pytest.fixture
def conns() -> Iterator[tuple[Connection, Connection]]:
    a, b = pair()
    yield a, b
    a.close(send_bye=False)
    b.close(send_bye=False)


def raw_pair() -> tuple[Connection, socket.socket]:
    """A started Connection (bob's side, responder) and the raw socket of a scripted peer."""
    sa, sb = socket.socketpair()
    sa.settimeout(WAIT)
    conn = Connection(sb, ALICE, False)
    conn.start()
    return conn, sa


def send_raw(sock: socket.socket, msg_type: MsgType, req_id: int, body: dict | bytes) -> None:
    send_msg(sock, msg_type, req_id, encode_body(msg_type, body))


def recv_raw(sock: socket.socket) -> tuple[MsgType, int, dict | bytes]:
    msg_type, req_id, data = recv_msg(sock)
    return msg_type, req_id, decode_body(msg_type, data)


def wait_until(cond: Callable[[], bool]) -> bool:
    deadline = time.monotonic() + WAIT
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.005)
    return cond()


# ---------------------------------------------------------------- establish()


def run_establish(
    ka: bytes, kb: bytes, ctx_a: object = None, ctx_b: object = None, b: PeerIdentity = BOB
) -> tuple[object, object, socket.socket, socket.socket]:
    sa, sb = socket.socketpair()
    sa.settimeout(WAIT * 4)
    sb.settimeout(WAIT * 4)
    out: dict[str, object] = {}

    def side(name: str, sock: socket.socket, key: bytes, me: PeerIdentity, init: bool, ctx: object):
        try:
            out[name] = establish(sock, key, me, initiator=init, tls_ctx=ctx)  # type: ignore
        except Exception as e:
            out[name] = e

    t = threading.Thread(target=side, args=("b", sb, kb, b, False, ctx_b))
    t.start()
    side("a", sa, ka, ALICE, True, ctx_a)
    t.join(10)
    assert not t.is_alive()
    return out["a"], out["b"], sa, sb


def test_establish_plain_success() -> None:
    a, b, _, _ = run_establish(KEY, KEY)
    assert isinstance(a, Connection) and isinstance(b, Connection)
    assert a.remote == BOB and b.remote == ALICE
    assert a.state is State.AUTHENTICATED and b.state is State.AUTHENTICATED
    a.close(send_bye=False)
    b.close(send_bye=False)


def test_establish_wrong_key_closes_sockets() -> None:
    a, b, sa, sb = run_establish(KEY, bytes(32))
    for result in (a, b):
        assert isinstance(result, ProtocolError) and result.code == ErrorCode.AUTH_FAILED
    assert sa.fileno() == -1 and sb.fileno() == -1


def test_establish_tls_success_and_traffic(certs: Path) -> None:
    ctx_a = tls.client_context(certs, "alice")
    ctx_b = tls.server_context(certs, "bob")
    a, b, _, _ = run_establish(KEY, KEY, ctx_a, ctx_b)
    assert isinstance(a, Connection) and isinstance(b, Connection)
    b.on(MsgType.INDEX_REQ, lambda c, rid, body: c.reply(rid, MsgType.INDEX, _index()))
    a.start()
    b.start()
    # Four threads write while both readers are blocked in recv on the same TLS sockets.
    futs: list[Future] = []
    lock = threading.Lock()

    def ask() -> None:
        f = a.request(MsgType.INDEX_REQ, {})
        with lock:
            futs.append(f)

    threads = [threading.Thread(target=ask) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(WAIT)
    assert all(f.result(WAIT)[0] == MsgType.INDEX for f in futs) and len(futs) == 4
    a.close()
    assert wait_until(lambda: b.closed)


def test_establish_tls_cert_name_must_match_hello(certs: Path) -> None:
    ctx_a = tls.client_context(certs, "alice")
    ctx_b = tls.server_context(certs, "bob")
    liar = PeerIdentity("B77C0E11", "carol")  # bob's cert, but HELLO claims to be carol
    a, b, sa, sb = run_establish(KEY, KEY, ctx_a, ctx_b, b=liar)
    # alice (initiator) sees bob's certificate CN "bob" != HELLO_ACK name "carol"
    assert isinstance(a, ProtocolError) and a.code == ErrorCode.AUTH_FAILED
    assert sa.fileno() == -1
    # bob checked alice's cert ("alice" == HELLO "alice"), so his side is established, but alice
    # sent ERROR AUTH_FAILED and closed: bob's reader sees it and closes.
    assert isinstance(b, Connection)
    b.start()
    assert wait_until(lambda: b.closed)


# ---------------------------------------------------------------- request / reply


def _index() -> dict:
    return {"index_rev": 3, "files": []}


def test_request_reply_round_trip(conns: tuple[Connection, Connection]) -> None:
    a, b = conns
    b.on(MsgType.INDEX_REQ, lambda c, rid, body: c.reply(rid, MsgType.INDEX, _index()))
    a.start()
    b.start()
    assert a.request(MsgType.INDEX_REQ, {}).result(WAIT) == (MsgType.INDEX, _index())


def test_out_of_order_replies_reach_their_own_future() -> None:
    conn, peer = raw_pair()
    futs = [conn.request(MsgType.CHUNK_REQ, {"hash": f"{i:x}" * 64}) for i in range(3)]
    seen = [recv_raw(peer) for _ in range(3)]
    ids = [rid for _, rid, _ in seen]
    assert len(set(ids)) == 3 and all(i % 2 == 0 for i in ids)  # responder: even ids
    for rid in reversed(ids):  # answer last request first
        send_raw(peer, MsgType.CHUNK_DATA, rid, str(rid).encode())
    assert [f.result(WAIT) for f in futs] == [
        (MsgType.CHUNK_DATA, str(rid).encode()) for rid in ids
    ]
    conn.close(send_bye=False)


def test_chunk_data_raw_bytes_round_trip(conns: tuple[Connection, Connection]) -> None:
    a, b = conns
    blob = bytes(range(256)) * 10
    b.on(MsgType.CHUNK_REQ, lambda c, rid, body: c.reply(rid, MsgType.CHUNK_DATA, blob))
    a.start()
    b.start()
    assert a.request(MsgType.CHUNK_REQ, {"hash": HASH}).result(WAIT) == (MsgType.CHUNK_DATA, blob)


def test_error_reply_raises_remote_error(conns: tuple[Connection, Connection]) -> None:
    a, b = conns

    def not_found(c: Connection, rid: int, body: object) -> None:
        c.reply(rid, MsgType.ERROR, {"code": ErrorCode.CHUNK_NOT_FOUND, "msg": "nope"})

    b.on(MsgType.CHUNK_REQ, not_found)
    a.start()
    b.start()
    with pytest.raises(RemoteError) as info:
        a.request(MsgType.CHUNK_REQ, {"hash": HASH}).result(WAIT)
    assert info.value.code == ErrorCode.CHUNK_NOT_FOUND and info.value.msg == "nope"
    assert not a.closed  # CHUNK_NOT_FOUND is not fatal (§6.8)


def test_request_ids_have_role_parity_and_do_not_clash_with_peer_requests() -> None:
    conn, peer = raw_pair()  # responder: our ids are even
    fut = conn.request(MsgType.INDEX_REQ, {})
    _, our_id, _ = recv_raw(peer)
    assert our_id == 2
    got: list[int] = []
    conn.on(
        MsgType.INDEX_REQ,
        lambda c, rid, body: (got.append(rid), c.reply(rid, MsgType.INDEX, _index())),
    )
    send_raw(peer, MsgType.INDEX_REQ, 1, {})  # peer's own request, odd id: a request, not a reply
    assert recv_raw(peer)[:2] == (MsgType.INDEX, 1)
    assert got == [1] and not fut.done()
    send_raw(peer, MsgType.INDEX, our_id, _index())
    assert fut.result(WAIT)[0] == MsgType.INDEX
    conn.close(send_bye=False)


def test_req_id_never_zero_and_wraps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(connection, "MAX_REQ_ID", 5)
    a, _ = pair()  # initiator: odd ids 1, 3, 5, then wraps
    with a._lock:
        ids = [a._alloc_id() for _ in range(7)]
    assert ids == [1, 3, 5, 1, 3, 5, 1]
    b_ids = []
    _, b = pair()
    with b._lock:
        b_ids = [b._alloc_id() for _ in range(5)]
    assert b_ids == [2, 4, 2, 4, 2]
    assert 0 not in ids + b_ids


def test_req_id_skips_ids_still_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(connection, "MAX_REQ_ID", 5)
    a, _ = pair()
    with a._lock:
        a._pending[3] = Future()
        assert [a._alloc_id() for _ in range(3)] == [1, 5, 1]


# ---------------------------------------------------------------- built-in behaviour


def test_ping_is_answered_with_pong_carrying_same_t() -> None:
    conn, peer = raw_pair()
    send_raw(peer, MsgType.PING, 9, {"t": 12.5})
    assert recv_raw(peer) == (MsgType.PONG, 9, {"t": 12.5})
    conn.close(send_bye=False)


def test_handshake_message_after_auth_is_illegal_state() -> None:
    conn, peer = raw_pair()
    send_raw(peer, MsgType.AUTH_OK, 0, {})
    msg_type, _, body = recv_raw(peer)
    assert msg_type == MsgType.ERROR and body["code"] == ErrorCode.ILLEGAL_STATE  # type: ignore
    assert wait_until(lambda: conn.closed)


def test_handler_exception_gives_error_internal_and_stays_open() -> None:
    conn, peer = raw_pair()

    def boom(c: Connection, rid: int, body: object) -> None:
        raise RuntimeError("bug")

    conn.on(MsgType.INDEX_REQ, boom)
    send_raw(peer, MsgType.INDEX_REQ, 1, {})
    msg_type, rid, body = recv_raw(peer)
    assert (msg_type, rid) == (MsgType.ERROR, 1) and body["code"] == ErrorCode.INTERNAL  # type: ignore
    send_raw(peer, MsgType.PING, 3, {"t": 1})  # still alive
    assert recv_raw(peer)[0] == MsgType.PONG
    assert not conn.closed
    conn.close(send_bye=False)


def test_no_handler_gives_error_internal_and_stays_open() -> None:
    conn, peer = raw_pair()
    send_raw(peer, MsgType.INDEX_REQ, 1, {})
    msg_type, rid, body = recv_raw(peer)
    assert (msg_type, rid) == (MsgType.ERROR, 1)
    assert body["code"] == ErrorCode.INTERNAL and body["msg"] == "unsupported"  # type: ignore
    assert not conn.closed
    conn.close(send_bye=False)


def test_fatal_error_from_peer_closes() -> None:
    conn, peer = raw_pair()
    send_raw(peer, MsgType.ERROR, 0, {"code": ErrorCode.BAD_VERSION, "msg": "x"})
    assert wait_until(lambda: conn.closed)


def test_malformed_body_sends_error_and_closes() -> None:
    conn, peer = raw_pair()
    send_msg(peer, MsgType.NOTIFY, 0, b"not json")
    msg_type, _, body = recv_raw(peer)
    assert msg_type == MsgType.ERROR and body["code"] == ErrorCode.BAD_HEADER  # type: ignore
    assert wait_until(lambda: conn.closed)


# ---------------------------------------------------------------- closing


def test_peer_close_fails_pending_and_fires_on_close_once() -> None:
    conn, peer = raw_pair()
    reasons: list[str] = []
    conn.on_close(lambda c, reason: reasons.append(reason))
    futs = [conn.request(MsgType.INDEX_REQ, {}) for _ in range(2)]
    peer.close()
    for f in futs:
        with pytest.raises(ConnectionError):
            f.result(WAIT)
    assert wait_until(lambda: len(reasons) == 1)
    conn.close()  # later closes do not fire it again
    time.sleep(0.05)
    assert len(reasons) == 1 and conn.state is State.CLOSED
    with pytest.raises(ConnectionError):
        conn.request(MsgType.INDEX_REQ, {})


def test_close_twice_is_safe_and_sends_one_bye() -> None:
    conn, peer = raw_pair()
    conn.close("done")
    conn.close("again")
    assert recv_raw(peer) == (MsgType.BYE, 0, {"reason": "done"})
    with pytest.raises(ConnectionError):  # EOF right after, no second BYE
        recv_raw(peer)


def test_received_bye_closes_without_sending_bye_back() -> None:
    conn, peer = raw_pair()
    send_raw(peer, MsgType.BYE, 0, {"reason": "bye"})
    assert wait_until(lambda: conn.closed)
    with pytest.raises(ConnectionError):
        recv_raw(peer)


def test_mark_synced() -> None:
    a, _ = pair()
    a.mark_synced()
    assert a.state is State.SYNCED
    a.close(send_bye=False)
    a.mark_synced()
    assert a.state is State.CLOSED


# ---------------------------------------------------------------- concurrency


def test_concurrent_sends_arrive_intact() -> None:
    conn, peer = raw_pair()
    peer.settimeout(WAIT * 4)
    payload = b"x" * 200_000  # large enough that sendall needs several writes
    n_threads, per_thread = 4, 5

    def spam() -> None:
        for _ in range(per_thread):
            conn.send(MsgType.CHUNK_DATA, payload, 7)

    threads = [threading.Thread(target=spam) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for _ in range(n_threads * per_thread):  # interleaved writes would break framing here
        assert recv_raw(peer) == (MsgType.CHUNK_DATA, 7, payload)
    for t in threads:
        t.join(WAIT)
    conn.close(send_bye=False)
