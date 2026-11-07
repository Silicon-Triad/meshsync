"""Tests for protocol/messages.py (§6.2, §6.4, §7.1)."""

import json

import pytest

from meshsync.protocol.constants import MAX_DATAGRAM, ErrorCode, MsgType
from meshsync.protocol.framing import ProtocolError
from meshsync.protocol.messages import (
    canonical_json,
    decode_announce,
    decode_body,
    encode_announce,
    encode_body,
    validate_entry,
    validate_path,
)

H = "ab" * 32  # 64 lowercase hex chars
NONCE = "0f" * 16
ENTRY = {
    "path": "notes/report.txt",
    "size": 183204,
    "file_hash": H,
    "chunks": [H, "cd" * 32],
    "version": {"A1F3C9D2": 4, "B77C0E11": 2},
    "deleted": False,
}

VALID = {
    MsgType.HELLO: {"peer_id": "A1F3C9D2", "name": "ahmed", "proto_ver": 1, "nonce_i": NONCE},
    MsgType.HELLO_ACK: {"peer_id": "B77C0E11", "name": "mujtaba", "nonce_r": NONCE, "mac_r": H},
    MsgType.AUTH: {"mac_i": H},
    MsgType.AUTH_OK: {},
    MsgType.INDEX_REQ: {},
    MsgType.INDEX: {"index_rev": 57, "files": [ENTRY]},
    MsgType.NOTIFY: {"entry": ENTRY},
    MsgType.CHUNK_REQ: {"hash": H},
    MsgType.PING: {"t": 1730473200.5},
    MsgType.PONG: {"t": 1730473200},
    MsgType.ERROR: {"code": 0x10, "msg": "no such chunk"},
    MsgType.BYE: {"reason": "shutdown"},
}


def test_every_json_type_has_an_example() -> None:
    assert set(VALID) == set(MsgType) - {MsgType.CHUNK_DATA}


@pytest.mark.parametrize("msg_type", list(VALID))
def test_round_trip(msg_type: MsgType) -> None:
    data = encode_body(msg_type, VALID[msg_type])
    assert isinstance(data, bytes)
    assert decode_body(msg_type, data) == VALID[msg_type]


def test_chunk_data_passes_through() -> None:
    raw = b"\x00\xffnot json"
    assert encode_body(MsgType.CHUNK_DATA, raw) == raw
    assert decode_body(MsgType.CHUNK_DATA, raw) == raw


def _bad(msg_type: MsgType, body: object) -> None:
    with pytest.raises(ProtocolError) as exc:
        decode_body(msg_type, json.dumps(body).encode())
    assert exc.value.code == ErrorCode.BAD_HEADER
    assert exc.value.fatal
    with pytest.raises(ProtocolError):
        encode_body(msg_type, body)  # type: ignore[arg-type]


def _with(msg_type: MsgType, **changes: object) -> dict:
    return {**VALID[msg_type], **changes}


def test_missing_key() -> None:
    _bad(MsgType.AUTH, {})


def test_unknown_key() -> None:
    _bad(MsgType.PING, _with(MsgType.PING, extra=1))


def test_not_an_object() -> None:
    _bad(MsgType.PING, [1])


@pytest.mark.parametrize(
    ("msg_type", "key", "value"),
    [
        (MsgType.HELLO, "proto_ver", "1"),
        (MsgType.HELLO, "proto_ver", True),
        (MsgType.HELLO, "name", 5),
        (MsgType.HELLO, "name", ""),
        (MsgType.PING, "t", "now"),
        (MsgType.PING, "t", True),
        (MsgType.ERROR, "msg", None),
        (MsgType.BYE, "reason", 0),
        (MsgType.INDEX, "index_rev", -1),
        (MsgType.INDEX, "index_rev", 1.5),
        (MsgType.INDEX, "files", {}),
    ],
)
def test_wrong_type(msg_type: MsgType, key: str, value: object) -> None:
    _bad(msg_type, _with(msg_type, **{key: value}))


@pytest.mark.parametrize("peer_id", ["a1f3c9d2", "A1F3C9D", "A1F3C9D2E", "G1F3C9D2"])
def test_bad_peer_id(peer_id: str) -> None:
    _bad(MsgType.HELLO, _with(MsgType.HELLO, peer_id=peer_id))


@pytest.mark.parametrize("nonce", ["0f" * 15, "0f" * 17, "0F" * 16, "zz" * 16])
def test_bad_nonce(nonce: str) -> None:
    _bad(MsgType.HELLO, _with(MsgType.HELLO, nonce_i=nonce))


@pytest.mark.parametrize("mac", [H[:-1], H + "a", H.upper(), "g" * 64])
def test_bad_mac(mac: str) -> None:
    _bad(MsgType.AUTH, {"mac_i": mac})


def test_bad_hash() -> None:
    _bad(MsgType.CHUNK_REQ, {"hash": H[:62]})


def test_bad_error_code() -> None:
    _bad(MsgType.ERROR, {"code": 0x42, "msg": "x"})


def test_invalid_json() -> None:
    for data in (b"{not json", b"\xff\xfe", b'{"t": NaN}'):
        with pytest.raises(ProtocolError) as exc:
            decode_body(MsgType.PING, data)
        assert exc.value.code == ErrorCode.BAD_HEADER


def test_index_validates_entries() -> None:
    _bad(MsgType.INDEX, _with(MsgType.INDEX, files=[{**ENTRY, "path": "../x"}]))


def test_notify_validates_entry() -> None:
    _bad(MsgType.NOTIFY, {"entry": {**ENTRY, "deleted": "no"}})


# ---------------------------------------------------------------- validate_entry


def test_validate_entry_ok() -> None:
    assert validate_entry(ENTRY) is ENTRY


@pytest.mark.parametrize(
    "change",
    [
        {"size": -1},
        {"file_hash": "x"},
        {"chunks": H},
        {"chunks": ["x"]},
        {"version": []},
        {"version": {"A1F3C9D2": -1}},
        {"version": {"A1F3C9D2": "4"}},
        {"version": {"bad": 1}},
        {"deleted": 0},
        {"extra": 1},
    ],
)
def test_validate_entry_rejects(change: dict) -> None:
    with pytest.raises(ProtocolError) as exc:
        validate_entry({**ENTRY, **change})
    assert exc.value.code == ErrorCode.BAD_HEADER


def test_validate_entry_missing_key() -> None:
    entry = dict(ENTRY)
    del entry["chunks"]
    with pytest.raises(ProtocolError):
        validate_entry(entry)


# ---------------------------------------------------------------- validate_path (security)


@pytest.mark.parametrize("path", ["a.txt", "dir/sub/f.bin", ".hidden", "a/.meshsync/x", "a..b"])
def test_validate_path_accepts(path: str) -> None:
    assert validate_path(path) == path


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/etc/passwd",
        "../x",
        "a/../b",
        "a/..",
        "..",
        "a//b",
        "a/",
        "./a",
        "a/./b",
        ".",
        "a\\b",
        "..\\x",
        ".meshsync",
        ".meshsync/x",
        ".meshsync/chunks/ab/" + H,
    ],
)
def test_validate_path_rejects(path: str) -> None:
    with pytest.raises(ProtocolError) as exc:
        validate_path(path)
    assert exc.value.code == ErrorCode.BAD_HEADER


def test_validate_path_rejects_non_str() -> None:
    with pytest.raises(ProtocolError):
        validate_path(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------- discovery datagram

ANNOUNCE = {
    "magic": "MS",
    "ver": 1,
    "type": "ANNOUNCE",
    "peer_id": "A1F3C9D2",
    "name": "ahmed",
    "tcp_port": 47001,
    "index_rev": 57,
    "ts": 1730473200,
    "tag": H,
}


def test_canonical_json_ignores_key_order() -> None:
    a = canonical_json({"b": 1, "a": [1, 2], "c": "é"})
    b = canonical_json({"c": "é", "a": [1, 2], "b": 1})
    assert a == b == '{"a":[1,2],"b":1,"c":"é"}'.encode()
    assert b" " not in a


def test_announce_round_trip() -> None:
    assert decode_announce(encode_announce(ANNOUNCE)) == ANNOUNCE
    leave = {**ANNOUNCE, "type": "LEAVE"}
    assert decode_announce(encode_announce(leave)) == leave


@pytest.mark.parametrize(
    "change",
    [
        {"type": "HELLO"},
        {"magic": "XX"},
        {"ver": 2},
        {"tcp_port": 0},
        {"ts": "now"},
        {"tag": "short"},
        {"extra": 1},
    ],
)
def test_announce_rejects(change: dict) -> None:
    bad = {**ANNOUNCE, **change}
    with pytest.raises(ValueError):
        encode_announce(bad)
    with pytest.raises(ValueError):
        decode_announce(canonical_json(bad))


def test_announce_missing_tag() -> None:
    fields = dict(ANNOUNCE)
    del fields["tag"]
    with pytest.raises(ValueError):
        encode_announce(fields)


def test_announce_oversize() -> None:
    big = {**ANNOUNCE, "name": "x" * MAX_DATAGRAM}
    with pytest.raises(ValueError):
        encode_announce(big)
    with pytest.raises(ValueError):
        decode_announce(canonical_json(big))


def test_announce_garbage() -> None:
    for data in (b"not json", b"\xff", b"[]"):
        with pytest.raises(ValueError):
            decode_announce(data)
