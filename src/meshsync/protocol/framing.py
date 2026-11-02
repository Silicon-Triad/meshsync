"""TCP framing: 12-byte header + body (PROJECT_CONTEXT.md §6.3).

Moves bytes only. No threads, no locks, no JSON (that lives in messages.py).
"""

import logging
import socket
import struct

from meshsync.protocol.constants import (
    FATAL_ERRORS,
    HEADER_FMT,
    HEADER_SIZE,
    MAGIC,
    MAX_BODY,
    MAX_REQ_ID,
    VERSION,
    ErrorCode,
    MsgType,
)

log = logging.getLogger(__name__)


class ProtocolError(Exception):
    """Carries an ErrorCode; caller decides whether to send ERROR and close."""

    def __init__(self, code: ErrorCode, msg: str = "") -> None:
        super().__init__(f"{code.name}: {msg}" if msg else code.name)
        self.code = code

    @property
    def fatal(self) -> bool:
        return self.code in FATAL_ERRORS


def pack_header(msg_type: int, req_id: int, body_len: int) -> bytes:
    """Build the 12-byte header."""
    if not 0 <= req_id <= MAX_REQ_ID:
        raise ValueError(f"req_id out of range: {req_id}")
    if body_len > MAX_BODY:
        raise ProtocolError(ErrorCode.TOO_LARGE, f"body {body_len} > {MAX_BODY}")
    return struct.pack(HEADER_FMT, MAGIC, VERSION, msg_type, req_id, body_len)


def unpack_header(data: bytes) -> tuple[MsgType, int, int]:
    """Parse and validate a header. Returns (type, req_id, body_len)."""
    if len(data) != HEADER_SIZE:
        raise ValueError(f"header must be {HEADER_SIZE} bytes, got {len(data)}")
    magic, version, type_code, req_id, body_len = struct.unpack(HEADER_FMT, data)
    if magic != MAGIC:
        raise ProtocolError(ErrorCode.BAD_HEADER, f"bad magic {magic!r}")
    if version != VERSION:
        raise ProtocolError(ErrorCode.BAD_VERSION, f"version {version}")
    try:
        msg_type = MsgType(type_code)
    except ValueError:
        raise ProtocolError(ErrorCode.BAD_HEADER, f"unknown type 0x{type_code:02X}") from None
    if body_len > MAX_BODY:
        raise ProtocolError(ErrorCode.TOO_LARGE, f"body {body_len} > {MAX_BODY}")
    return msg_type, req_id, body_len


def read_exact(sock: socket.socket, n: int) -> bytes:
    """Read exactly n bytes; recv may return fewer, so loop."""
    buf = bytearray()
    while len(buf) < n:
        part = sock.recv(n - len(buf))
        if not part:
            raise ConnectionError("peer closed connection")
        buf += part
    return bytes(buf)


def send_msg(sock: socket.socket, msg_type: int, req_id: int, body: bytes = b"") -> None:
    """Send header + body in one sendall."""
    if not isinstance(body, bytes):
        raise TypeError("body must be bytes")
    sock.sendall(pack_header(msg_type, req_id, len(body)) + body)
    log.debug("sent type=0x%02X req_id=%d len=%d", msg_type, req_id, len(body))


def recv_msg(sock: socket.socket) -> tuple[MsgType, int, bytes]:
    """Receive one message. Returns (type, req_id, body)."""
    msg_type, req_id, body_len = unpack_header(read_exact(sock, HEADER_SIZE))
    body = read_exact(sock, body_len)
    log.debug("recv type=%s req_id=%d len=%d", msg_type.name, req_id, body_len)
    return msg_type, req_id, body
