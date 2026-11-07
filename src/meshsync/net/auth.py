"""HMAC-SHA256 challenge-response handshake (PROJECT_CONTEXT.md §6.5, D4).

Owner: Muhammad Ahmed Qazi (CS-24045). Issue #4.
Contract (PROJECT_CONTEXT.md §8.3):
    Initiator and responder sides of HELLO/HELLO_ACK/AUTH/AUTH_OK (§6.5).
    mac_r = HMAC(K, 'R'|nonce_i|nonce_r), mac_i = HMAC(K, 'I'|nonce_r|nonce_i).
    Compare with hmac.compare_digest; HANDSHAKE_TIMEOUT; AUTH_FAILED on mismatch.

The drivers are blocking and run on the raw (or TLS-wrapped) socket BEFORE Connection starts its
reader thread. All handshake messages use REQ_ID 0 (PROTOCOL.md §5).
"""

import hashlib
import hmac
import logging
import secrets
import socket
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from meshsync.protocol.constants import HANDSHAKE_TIMEOUT, NONCE_BYTES, VERSION, ErrorCode, MsgType
from meshsync.protocol.framing import ProtocolError, recv_msg, send_msg
from meshsync.protocol.messages import decode_body, encode_body

log = logging.getLogger(__name__)

HANDSHAKE_REQ_ID = 0  # handshake messages are not request/response pairs (PROTOCOL.md §5)
SECRET_BYTES = 32  # shared secret file: 64 hex chars (issue #4)


@dataclass(frozen=True)
class PeerIdentity:
    peer_id: str
    name: str


class _PeerSentError(ProtocolError):
    """The other side sent ERROR: we raise it locally but must not answer with another ERROR."""


# ---------------------------------------------------------------- pure functions (no sockets)


def load_secret(path: Path) -> bytes:
    """Read the shared secret K: one line of hex, 32 bytes. Never log the value."""
    text = path.read_text(encoding="ascii").strip()
    try:
        key = bytes.fromhex(text)
    except ValueError:
        raise ValueError(f"secret file {path} is not hex") from None
    if len(key) != SECRET_BYTES:
        raise ValueError(f"secret file {path} must hold {SECRET_BYTES * 2} hex characters")
    return key


def new_nonce() -> str:
    """Fresh random challenge, 16 bytes as hex (§6.5)."""
    return secrets.token_hex(NONCE_BYTES)


def _mac(key: bytes, role: bytes, first: str, second: str) -> str:
    # PROTOCOL.md decision (g): role byte + raw nonce bytes. The role prefix makes the two MACs
    # differ, so an attacker cannot reflect one side's MAC back as the other side's proof.
    data = role + bytes.fromhex(first) + bytes.fromhex(second)
    return hmac.new(key, data, hashlib.sha256).hexdigest()


def responder_mac(key: bytes, nonce_i: str, nonce_r: str) -> str:
    """mac_r = HMAC-SHA256(K, "R" | nonce_i | nonce_r) (§6.5)."""
    return _mac(key, b"R", nonce_i, nonce_r)


def initiator_mac(key: bytes, nonce_r: str, nonce_i: str) -> str:
    """mac_i = HMAC-SHA256(K, "I" | nonce_r | nonce_i) (§6.5)."""
    return _mac(key, b"I", nonce_r, nonce_i)


def verify(expected: str, received: str) -> bool:
    """Constant-time comparison, so timing does not leak how many characters matched (§6.5)."""
    return hmac.compare_digest(expected, received)


# ---------------------------------------------------------------- socket helpers


def _send(sock: socket.socket, msg_type: MsgType, body: dict) -> None:
    send_msg(sock, msg_type, HANDSHAKE_REQ_ID, encode_body(msg_type, body))


def _expect(sock: socket.socket, want: MsgType) -> dict:
    """Receive the next message; it must be `want` (§6.7), else raise ProtocolError."""
    msg_type, _req_id, data = recv_msg(sock)
    if msg_type == MsgType.ERROR:
        body = decode_body(MsgType.ERROR, data)
        raise _PeerSentError(ErrorCode(body["code"]), f"peer sent ERROR: {body['msg']}")
    if msg_type != want:
        raise ProtocolError(ErrorCode.ILLEGAL_STATE, f"expected {want.name}, got {msg_type.name}")
    return decode_body(msg_type, data)


def _send_error(sock: socket.socket, err: ProtocolError) -> None:
    """Best effort: tell the peer why before the caller closes (§6.8)."""
    if isinstance(err, _PeerSentError):
        return
    try:
        _send(sock, MsgType.ERROR, {"code": int(err.code), "msg": str(err)})
    except OSError as e:
        log.debug("could not send ERROR: %s", e)


def _run(sock: socket.socket, role: str, steps: Callable[[], PeerIdentity]) -> PeerIdentity:
    """Run steps() under HANDSHAKE_TIMEOUT; log and report failures; restore the old timeout."""
    previous = sock.gettimeout()
    sock.settimeout(HANDSHAKE_TIMEOUT)  # §6.5: whole handshake within 10 s
    try:
        remote = steps()
    except ProtocolError as e:
        log.warning("%s handshake failed: %s", role, e)
        _send_error(sock, e)
        raise
    except OSError as e:  # includes ConnectionError and socket.timeout; caller closes
        log.warning("%s handshake failed: %s", role, e)
        raise
    finally:
        sock.settimeout(previous)
    log.info("%s handshake ok with %s (%s)", role, remote.name, remote.peer_id)
    return remote


def _check_not_self(me: PeerIdentity, remote_peer_id: str) -> None:
    if remote_peer_id == me.peer_id:
        raise ProtocolError(ErrorCode.AUTH_FAILED, "self-connection")


# ---------------------------------------------------------------- handshake drivers (§6.5)


def initiator_handshake(sock: socket.socket, key: bytes, me: PeerIdentity) -> PeerIdentity:
    """Dialling side: HELLO -> HELLO_ACK -> AUTH -> AUTH_OK. Returns the remote identity."""

    def steps() -> PeerIdentity:
        nonce_i = new_nonce()
        hello = {"peer_id": me.peer_id, "name": me.name, "proto_ver": VERSION, "nonce_i": nonce_i}
        _send(sock, MsgType.HELLO, hello)

        ack = _expect(sock, MsgType.HELLO_ACK)
        _check_not_self(me, ack["peer_id"])
        if not verify(responder_mac(key, nonce_i, ack["nonce_r"]), ack["mac_r"]):
            raise ProtocolError(ErrorCode.AUTH_FAILED, "bad mac_r")

        _send(sock, MsgType.AUTH, {"mac_i": initiator_mac(key, ack["nonce_r"], nonce_i)})
        _expect(sock, MsgType.AUTH_OK)
        return PeerIdentity(ack["peer_id"], ack["name"])

    return _run(sock, "initiator", steps)


def responder_handshake(sock: socket.socket, key: bytes, me: PeerIdentity) -> PeerIdentity:
    """Accepting side: HELLO -> HELLO_ACK -> AUTH -> AUTH_OK. Returns the remote identity."""

    def steps() -> PeerIdentity:
        hello = _expect(sock, MsgType.HELLO)
        if hello["proto_ver"] != VERSION:
            raise ProtocolError(ErrorCode.BAD_VERSION, f"proto_ver {hello['proto_ver']}")
        _check_not_self(me, hello["peer_id"])

        nonce_r = new_nonce()
        mac_r = responder_mac(key, hello["nonce_i"], nonce_r)
        ack = {"peer_id": me.peer_id, "name": me.name, "nonce_r": nonce_r, "mac_r": mac_r}
        _send(sock, MsgType.HELLO_ACK, ack)

        auth = _expect(sock, MsgType.AUTH)
        if not verify(initiator_mac(key, nonce_r, hello["nonce_i"]), auth["mac_i"]):
            raise ProtocolError(ErrorCode.AUTH_FAILED, "bad mac_i")

        _send(sock, MsgType.AUTH_OK, {})
        return PeerIdentity(hello["peer_id"], hello["name"])

    return _run(sock, "responder", steps)
