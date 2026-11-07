"""Message body codecs and validators (PROJECT_CONTEXT.md §6.2, §6.4, §7.1).

Framing moves bytes; this module turns bodies into validated Python values and back.
No sockets, no threads, no I/O.

Owner: Muhammad Ahmed Qazi (CS-24045). Issue #31.

Design: each field has a small checker that raises ValueError. Public TCP functions turn that into
ProtocolError(BAD_HEADER) (PROTOCOL.md decision (c)); the UDP announce codec keeps ValueError,
because UDP has no ERROR reply.
"""

import json
import logging
import re
from collections.abc import Callable
from typing import Any

from meshsync.protocol.constants import (
    EMPTY_FILE_HASH,
    MAGIC,
    MAX_DATAGRAM,
    META_DIR,
    NONCE_BYTES,
    PEER_ID_HEX_LEN,
    SHA256_HEX_LEN,
    VERSION,
    ErrorCode,
    MsgType,
)
from meshsync.protocol.framing import ProtocolError

log = logging.getLogger(__name__)

Checker = Callable[[Any], None]

_PEER_ID_RE = re.compile(rf"[0-9A-F]{{{PEER_ID_HEX_LEN}}}")  # §6.2 example: "A1F3C9D2"
_SHA256_RE = re.compile(rf"[0-9a-f]{{{SHA256_HEX_LEN}}}")  # hashlib .hexdigest() is lowercase
_NONCE_RE = re.compile(rf"[0-9a-f]{{{NONCE_BYTES * 2}}}")  # §6.5: 16 bytes as hex
# Hex case is fixed by PROTOCOL.md decision (d): peer_id uppercase, all other hex lowercase.


# ---------------------------------------------------------------- field checkers (raise ValueError)


def _is_int(value: Any) -> bool:
    # bool is a subclass of int in Python; true/false must not pass as numbers.
    return isinstance(value, int) and not isinstance(value, bool)


def _check_int(value: Any) -> None:
    if not _is_int(value):
        raise ValueError(f"expected int, got {type(value).__name__}")


def _check_uint(value: Any) -> None:
    _check_int(value)
    if value < 0:
        raise ValueError(f"expected int >= 0, got {value}")


def _check_number(value: Any) -> None:
    if not (_is_int(value) or isinstance(value, float)):
        raise ValueError(f"expected number, got {type(value).__name__}")


def _check_str(value: Any) -> None:
    if not isinstance(value, str):
        raise ValueError(f"expected str, got {type(value).__name__}")


def _check_name(value: Any) -> None:
    _check_str(value)
    if not value:
        raise ValueError("name must not be empty")


def _pattern(regex: re.Pattern[str], what: str) -> Checker:
    """Build a checker for a string that must fully match regex."""

    def check(value: Any) -> None:
        if not isinstance(value, str) or not regex.fullmatch(value):
            raise ValueError(f"bad {what}: {value!r}")

    return check


_check_peer_id = _pattern(_PEER_ID_RE, "peer_id")
_check_sha256 = _pattern(_SHA256_RE, "sha256 hex")
_check_nonce = _pattern(_NONCE_RE, "nonce")


def _check_error_code(value: Any) -> None:
    _check_int(value)
    try:
        ErrorCode(value)
    except ValueError:
        raise ValueError(f"unknown error code {value}") from None


def _check_fields(obj: Any, schema: dict[str, Checker]) -> None:
    """obj must be a dict with exactly the keys of schema, each passing its checker."""
    if not isinstance(obj, dict):
        raise ValueError(f"expected object, got {type(obj).__name__}")
    missing = schema.keys() - obj.keys()
    if missing:
        raise ValueError(f"missing keys {sorted(missing)}")
    unknown = obj.keys() - schema.keys()
    if unknown:
        raise ValueError(f"unknown keys {sorted(unknown)}")
    for key, check in schema.items():
        try:
            check(obj[key])
        except ValueError as e:
            raise ValueError(f"{key}: {e}") from None


# ---------------------------------------------------------------- index entry and path (§7.1)


def _check_path(path: Any) -> None:
    _check_str(path)
    if not path:
        raise ValueError("empty path")
    if "\\" in path:
        raise ValueError("backslash in path")
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in path):
        # NUL would make open() fail later; other control chars are never legitimate names.
        raise ValueError("control character in path")
    if path.startswith("/"):
        raise ValueError("absolute path")
    segments = path.split("/")
    for seg in segments:
        if seg in ("", ".", ".."):
            raise ValueError(f"bad segment {seg!r}")
    if segments[0] == META_DIR:  # §7.3: .meshsync/ is never synced
        raise ValueError(f"path inside {META_DIR}/")


def _check_chunks(value: Any) -> None:
    if not isinstance(value, list):
        raise ValueError("chunks must be a list")
    for h in value:
        _check_sha256(h)


def _check_version(value: Any) -> None:
    """Version vector: peer_id -> counter >= 0 (§7.1, §7.2)."""
    if not isinstance(value, dict):
        raise ValueError("version must be an object")
    for peer_id, counter in value.items():
        _check_peer_id(peer_id)
        _check_uint(counter)


def _check_bool(value: Any) -> None:
    if not isinstance(value, bool):
        raise ValueError(f"expected bool, got {type(value).__name__}")


_ENTRY_SCHEMA: dict[str, Checker] = {
    "path": _check_path,
    "size": _check_uint,
    "file_hash": _check_sha256,
    "chunks": _check_chunks,
    "version": _check_version,
    "deleted": _check_bool,
}


def _check_entry(entry: Any) -> None:
    """Shape and formats only. Consistency of size/chunks/file_hash is the engine's job (sync/)."""
    _check_fields(entry, _ENTRY_SCHEMA)
    # PROTOCOL.md decision (f): a tombstone carries no content.
    if entry["deleted"] and (
        entry["size"] != 0 or entry["chunks"] != [] or entry["file_hash"] != EMPTY_FILE_HASH
    ):
        raise ValueError("tombstone must have size 0, chunks [] and the empty-file hash")


def _check_files(value: Any) -> None:
    if not isinstance(value, list):
        raise ValueError("files must be a list")
    for i, entry in enumerate(value):
        try:
            _check_entry(entry)
        except ValueError as e:
            raise ValueError(f"[{i}] {e}") from None


def validate_path(path: str) -> str:
    """Return path if it is a safe POSIX relative path inside the sync root (§7.1).

    Security boundary: a remote peer controls this string, so anything that could escape the
    root (absolute, '..', backslash) or touch our metadata (.meshsync/) is rejected.
    """
    try:
        _check_path(path)
    except ValueError as e:
        raise ProtocolError(ErrorCode.BAD_HEADER, f"path: {e}") from None
    return path


def validate_entry(entry: dict) -> dict:
    """Return entry if it is a valid index entry (§7.1), else raise ProtocolError(BAD_HEADER)."""
    try:
        _check_entry(entry)
    except ValueError as e:
        raise ProtocolError(ErrorCode.BAD_HEADER, f"entry: {e}") from None
    return entry


# ---------------------------------------------------------------- TCP bodies (§6.4)

_BODY_SCHEMAS: dict[MsgType, dict[str, Checker]] = {
    MsgType.HELLO: {
        "peer_id": _check_peer_id,
        "name": _check_name,
        "proto_ver": _check_int,
        "nonce_i": _check_nonce,
    },
    MsgType.HELLO_ACK: {
        "peer_id": _check_peer_id,
        "name": _check_name,
        "nonce_r": _check_nonce,
        "mac_r": _check_sha256,
    },
    MsgType.AUTH: {"mac_i": _check_sha256},
    MsgType.AUTH_OK: {},
    MsgType.INDEX_REQ: {},
    MsgType.INDEX: {"index_rev": _check_uint, "files": _check_files},
    MsgType.NOTIFY: {"entry": _check_entry},
    MsgType.CHUNK_REQ: {"hash": _check_sha256},
    MsgType.PING: {"t": _check_number},
    MsgType.PONG: {"t": _check_number},
    MsgType.ERROR: {"code": _check_error_code, "msg": _check_str},
    MsgType.BYE: {"reason": _check_str},
}


def _validate_body(msg_type: MsgType, body: Any) -> None:
    try:
        _check_fields(body, _BODY_SCHEMAS[msg_type])
    except ValueError as e:
        raise ProtocolError(ErrorCode.BAD_HEADER, f"{msg_type.name} body: {e}") from None


def _parse_json(data: bytes) -> Any:
    """UTF-8 JSON -> Python value. Raises ValueError on any malformed input.

    Duplicate keys are accepted and the last value wins (json module default); the result is then
    validated like any other body, so this cannot smuggle in an invalid value.
    """
    try:
        return json.loads(data.decode("utf-8"), parse_constant=_reject_constant)
    except RecursionError:
        # Deeply nested input (e.g. b"[" * 200000) exhausts the parser's stack; it is not a
        # ValueError, so convert it or it would escape the caller's error handling.
        raise ValueError("JSON nested too deeply") from None


def _reject_constant(name: str) -> None:
    # json.loads accepts NaN/Infinity by default; they are not valid JSON.
    raise ValueError(f"invalid JSON constant {name}")


def encode_body(msg_type: MsgType, body: dict | bytes) -> bytes:
    """Validate body, then encode as UTF-8 JSON (§6.3).

    CHUNK_DATA bodies are raw chunk bytes, not JSON: they are returned unchanged.
    """
    if msg_type == MsgType.CHUNK_DATA:
        if not isinstance(body, bytes):
            raise TypeError("CHUNK_DATA body must be bytes")
        return body
    _validate_body(msg_type, body)
    try:
        # allow_nan=False: NaN/Infinity are not JSON and our own decoder rejects them.
        return json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except ValueError as e:
        raise ProtocolError(ErrorCode.BAD_HEADER, f"{msg_type.name} body: {e}") from None


def decode_body(msg_type: MsgType, data: bytes) -> dict | bytes:
    """Parse a UTF-8 JSON body, then validate it. Raises ProtocolError(BAD_HEADER) if invalid.

    CHUNK_DATA bodies are raw chunk bytes, not JSON: they are returned unchanged.
    """
    if msg_type == MsgType.CHUNK_DATA:
        return data
    try:
        body = _parse_json(data)
    except ValueError as e:  # UnicodeDecodeError and JSONDecodeError are ValueErrors
        raise ProtocolError(ErrorCode.BAD_HEADER, f"{msg_type.name} body not JSON: {e}") from None
    _validate_body(msg_type, body)
    return body


# ---------------------------------------------------------------- discovery datagram (§6.2)


def canonical_json(obj: dict) -> bytes:
    """Deterministic JSON encoding of obj.

    The announce tag is HMAC-SHA256(K, canonical_json_without_tag) (§6.2). Sender and receiver
    each compute it over their own encoding, so both must produce identical bytes for the same
    fields: keys sorted, no whitespace, non-ASCII kept as UTF-8.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _check_magic(value: Any) -> None:
    if value != MAGIC.decode("ascii"):
        raise ValueError(f"bad magic {value!r}")


def _check_ver(value: Any) -> None:
    if not _is_int(value) or value != VERSION:
        raise ValueError(f"bad ver {value!r}")


def _check_announce_type(value: Any) -> None:
    if value not in ("ANNOUNCE", "LEAVE"):
        raise ValueError(f"bad type {value!r}")


def _check_port(value: Any) -> None:
    _check_int(value)
    if not 1 <= value <= 65535:  # TCP port range
        raise ValueError(f"bad port {value}")


_ANNOUNCE_SCHEMA: dict[str, Checker] = {
    "magic": _check_magic,
    "ver": _check_ver,
    "type": _check_announce_type,
    "peer_id": _check_peer_id,
    "name": _check_name,
    "tcp_port": _check_port,
    "index_rev": _check_uint,
    "ts": _check_int,
    "tag": _check_sha256,
}


def encode_announce(fields: dict) -> bytes:
    """Validate an ANNOUNCE/LEAVE datagram (tag included) and encode it. Raises ValueError.

    Shape only: computing and checking the HMAC tag and ts freshness is net/discovery.py's job.
    """
    _check_fields(fields, _ANNOUNCE_SCHEMA)
    data = canonical_json(fields)
    if len(data) > MAX_DATAGRAM:
        raise ValueError(f"datagram {len(data)} > {MAX_DATAGRAM} bytes")
    return data


def decode_announce(data: bytes) -> dict:
    """Parse and shape-check a received datagram. Raises ValueError (UDP has no ERROR reply)."""
    if len(data) > MAX_DATAGRAM:
        raise ValueError(f"datagram {len(data)} > {MAX_DATAGRAM} bytes")
    fields = _parse_json(data)
    _check_fields(fields, _ANNOUNCE_SCHEMA)
    return fields
