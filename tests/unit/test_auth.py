import socket
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from meshsync.net import auth
from meshsync.net.auth import (
    PeerIdentity,
    initiator_handshake,
    initiator_mac,
    load_secret,
    new_nonce,
    responder_handshake,
    responder_mac,
    verify,
)
from meshsync.protocol.constants import NONCE_BYTES, ErrorCode, MsgType
from meshsync.protocol.framing import ProtocolError, recv_msg, send_msg
from meshsync.protocol.messages import decode_body, encode_body

KEY = bytes(range(32))
OTHER_KEY = bytes(32)
NONCE_I = "00" * NONCE_BYTES
NONCE_R = "11" * NONCE_BYTES
ALICE = PeerIdentity("A1F3C9D2", "ahmed")
BOB = PeerIdentity("B77C0E11", "mujtaba")


def run_pair(
    left: Callable[[socket.socket], object], right: Callable[[socket.socket], object]
) -> tuple[object, object]:
    """Run left(sock) in a thread and right(sock) here; return (result or exception) of each."""
    a, b = socket.socketpair()
    a.settimeout(5)
    b.settimeout(5)
    results: dict[str, object] = {}

    def call(name: str, fn: Callable[[socket.socket], object], sock: socket.socket) -> None:
        try:
            results[name] = fn(sock)
        except Exception as e:  # collected and asserted by the test
            results[name] = e
        finally:
            sock.close()  # unblock the other side if it is still waiting

    t = threading.Thread(target=call, args=("left", left, a))
    t.start()
    call("right", right, b)
    t.join(5)
    return results["left"], results["right"]


def initiator(key: bytes, me: PeerIdentity) -> Callable[[socket.socket], object]:
    return lambda s: initiator_handshake(s, key, me)


def responder(key: bytes, me: PeerIdentity) -> Callable[[socket.socket], object]:
    return lambda s: responder_handshake(s, key, me)


def assert_code(result: object, code: ErrorCode) -> None:
    assert isinstance(result, ProtocolError), result
    assert result.code == code


# ---------------------------------------------------------------- pure functions


def test_mac_known_answer() -> None:
    assert (
        responder_mac(KEY, NONCE_I, NONCE_R)
        == "d9e4a5762809333bc39c417aa751d5f4a383bb281273b870d46d2dd03ee0b4d3"
    )
    assert (
        initiator_mac(KEY, NONCE_R, NONCE_I)
        == "fd99f533b69d7a630f5c48fc8f6b0b0f2fd32bb51411446f99adcfff7aa626b5"
    )


def test_role_prefix_makes_macs_differ() -> None:
    # Same key and same nonce order: only the "R"/"I" prefix differs.
    assert responder_mac(KEY, NONCE_I, NONCE_R) != initiator_mac(KEY, NONCE_I, NONCE_R)


def test_verify() -> None:
    mac = responder_mac(KEY, NONCE_I, NONCE_R)
    assert verify(mac, mac)
    assert not verify(mac, "0" * len(mac))


def test_new_nonce_is_hex_of_right_length() -> None:
    nonce = new_nonce()
    assert len(bytes.fromhex(nonce)) == NONCE_BYTES
    assert new_nonce() != nonce


def test_load_secret_valid(tmp_path: Path) -> None:
    path = tmp_path / "key"
    path.write_text(KEY.hex() + "\n")
    assert load_secret(path) == KEY


@pytest.mark.parametrize("content", ["zz" * 32, "ab" * 16, "ab" * 33, "", "abc"])
def test_load_secret_rejects_bad(tmp_path: Path, content: str) -> None:
    path = tmp_path / "key"
    path.write_text(content)
    with pytest.raises(ValueError):
        load_secret(path)


# ---------------------------------------------------------------- handshake


def test_handshake_success() -> None:
    left, right = run_pair(initiator(KEY, ALICE), responder(KEY, BOB))
    assert left == BOB
    assert right == ALICE


def test_timeout_restored_after_success() -> None:
    def init(s: socket.socket) -> object:
        s.settimeout(7.5)
        initiator_handshake(s, KEY, ALICE)
        return s.gettimeout()

    left, right = run_pair(init, responder(KEY, BOB))
    assert left == 7.5
    assert right == ALICE


def test_wrong_secret_on_initiator() -> None:
    left, right = run_pair(initiator(OTHER_KEY, ALICE), responder(KEY, BOB))
    assert_code(left, ErrorCode.AUTH_FAILED)  # initiator rejects mac_r
    assert_code(right, ErrorCode.AUTH_FAILED)  # responder gets ERROR instead of AUTH


def test_wrong_secret_on_responder() -> None:
    left, right = run_pair(initiator(KEY, ALICE), responder(OTHER_KEY, BOB))
    assert_code(left, ErrorCode.AUTH_FAILED)


def test_wrong_mac_i_rejected_by_responder() -> None:
    def bad_initiator(s: socket.socket) -> object:
        hello = {"peer_id": ALICE.peer_id, "name": "x", "proto_ver": 1, "nonce_i": NONCE_I}
        send_msg(s, MsgType.HELLO, 0, encode_body(MsgType.HELLO, hello))
        recv_msg(s)  # HELLO_ACK
        send_msg(s, MsgType.AUTH, 0, encode_body(MsgType.AUTH, {"mac_i": "0" * 64}))
        return recv_msg(s)

    left, right = run_pair(bad_initiator, responder(KEY, BOB))
    assert_code(right, ErrorCode.AUTH_FAILED)
    msg_type, req_id, body = left
    assert (msg_type, req_id) == (MsgType.ERROR, 0)
    assert decode_body(MsgType.ERROR, body)["code"] == ErrorCode.AUTH_FAILED


def test_wrong_proto_ver() -> None:
    def old_initiator(s: socket.socket) -> object:
        hello = {"peer_id": ALICE.peer_id, "name": "x", "proto_ver": 2, "nonce_i": NONCE_I}
        send_msg(s, MsgType.HELLO, 0, encode_body(MsgType.HELLO, hello))
        return recv_msg(s)

    left, right = run_pair(old_initiator, responder(KEY, BOB))
    assert_code(right, ErrorCode.BAD_VERSION)
    msg_type, _, body = left
    assert msg_type == MsgType.ERROR
    assert decode_body(MsgType.ERROR, body)["code"] == ErrorCode.BAD_VERSION


def test_out_of_order_auth_first() -> None:
    def rude_initiator(s: socket.socket) -> object:
        send_msg(s, MsgType.AUTH, 0, encode_body(MsgType.AUTH, {"mac_i": "0" * 64}))
        return recv_msg(s)

    left, right = run_pair(rude_initiator, responder(KEY, BOB))
    assert_code(right, ErrorCode.ILLEGAL_STATE)
    assert left[0] == MsgType.ERROR


def test_self_connection() -> None:
    left, right = run_pair(initiator(KEY, ALICE), responder(KEY, ALICE))
    assert_code(right, ErrorCode.AUTH_FAILED)  # responder sees its own peer_id in HELLO
    assert_code(left, ErrorCode.AUTH_FAILED)  # initiator gets the ERROR


def test_silent_peer_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth, "HANDSHAKE_TIMEOUT", 0.2)
    a, b = socket.socketpair()
    try:
        with pytest.raises(TimeoutError):  # socket.timeout is TimeoutError on 3.10+
            responder_handshake(a, KEY, BOB)
    finally:
        a.close()
        b.close()
