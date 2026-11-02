import socket
import struct
import threading

import pytest

from meshsync.protocol.constants import (
    HEADER_FMT,
    HEADER_SIZE,
    MAGIC,
    MAX_BODY,
    VERSION,
    ErrorCode,
    MsgType,
)
from meshsync.protocol.framing import (
    ProtocolError,
    pack_header,
    read_exact,
    recv_msg,
    send_msg,
    unpack_header,
)


@pytest.fixture
def pair():
    a, b = socket.socketpair()
    a.settimeout(5)
    b.settimeout(5)
    yield a, b
    a.close()
    b.close()


def crafted(magic=MAGIC, ver=VERSION, type_code=MsgType.PING, req_id=0, body_len=0) -> bytes:
    return struct.pack(HEADER_FMT, magic, ver, type_code, req_id, body_len)


def test_pack_unpack_round_trip():
    header = pack_header(MsgType.CHUNK_REQ, 42, 100)
    assert len(header) == HEADER_SIZE
    assert unpack_header(header) == (MsgType.CHUNK_REQ, 42, 100)


def test_header_is_big_endian():
    header = pack_header(MsgType.PING, 1, 0x0102)
    assert header[0:2] == b"MS"
    assert header[2] == 1
    assert header[3] == 0x30
    assert header[4:8] == b"\x00\x00\x00\x01"
    assert header[8:12] == b"\x00\x00\x01\x02"


@pytest.mark.parametrize("body", [b"", b'{"t": 1}', b"\xab" * MAX_BODY])
def test_send_recv_round_trip(pair, body):
    a, b = pair
    # A large body would fill the socket buffer, so send from a thread.
    t = threading.Thread(target=send_msg, args=(a, MsgType.CHUNK_DATA, 7, body))
    t.start()
    assert recv_msg(b) == (MsgType.CHUNK_DATA, 7, body)
    t.join()


def test_bad_magic():
    with pytest.raises(ProtocolError) as e:
        unpack_header(crafted(magic=b"XX"))
    assert e.value.code == ErrorCode.BAD_HEADER


def test_bad_version():
    with pytest.raises(ProtocolError) as e:
        unpack_header(crafted(ver=2))
    assert e.value.code == ErrorCode.BAD_VERSION


def test_unknown_type():
    with pytest.raises(ProtocolError) as e:
        unpack_header(crafted(type_code=0x55))
    assert e.value.code == ErrorCode.BAD_HEADER


def test_body_len_too_large():
    with pytest.raises(ProtocolError) as e:
        unpack_header(crafted(body_len=MAX_BODY + 1))
    assert e.value.code == ErrorCode.TOO_LARGE


def test_wrong_header_length():
    with pytest.raises(ValueError):
        unpack_header(b"MS")


def test_oversize_body_on_send(pair):
    a, _ = pair
    with pytest.raises(ProtocolError) as e:
        send_msg(a, MsgType.CHUNK_DATA, 1, b"\x00" * (MAX_BODY + 1))
    assert e.value.code == ErrorCode.TOO_LARGE


@pytest.mark.parametrize("req_id", [-1, 2**32])
def test_req_id_out_of_range(req_id):
    with pytest.raises(ValueError):
        pack_header(MsgType.PING, req_id, 0)


def test_body_must_be_bytes(pair):
    a, _ = pair
    with pytest.raises(TypeError):
        send_msg(a, MsgType.PING, 1, "not bytes")  # type: ignore[arg-type]


def test_read_exact_trickled(pair):
    a, b = pair
    data = pack_header(MsgType.PONG, 3, 4) + b"abcd"

    def trickle():
        for i in range(len(data)):
            a.sendall(data[i : i + 1])

    t = threading.Thread(target=trickle)
    t.start()
    assert read_exact(b, len(data)) == data
    t.join()


def test_close_mid_header(pair):
    a, b = pair
    a.sendall(pack_header(MsgType.PING, 1, 0)[:5])
    a.close()
    with pytest.raises(ConnectionError):
        recv_msg(b)


def test_close_mid_body(pair):
    a, b = pair
    a.sendall(pack_header(MsgType.CHUNK_DATA, 1, 10) + b"abc")
    a.close()
    with pytest.raises(ConnectionError):
        recv_msg(b)


def test_protocol_error_fatal_flag():
    assert ProtocolError(ErrorCode.AUTH_FAILED).fatal is True
    assert ProtocolError(ErrorCode.CHUNK_NOT_FOUND).fatal is False
