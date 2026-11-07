# FROZEN INTERFACE (AGENTS.md §6): changing or adding to the public interface in this file
# needs an issue and approval from all three members first (R-6.2).
"""Protocol constants: ports, limits, timers, message types and error codes.

Single home for every value from PROJECT_CONTEXT.md §6 and §7. Nothing else may hard-code these.
"""

import hashlib
import struct
from enum import IntEnum

# Network (§6.1, §6.2)
UDP_PORT = 47000
TCP_PORT = 47001
MAX_DATAGRAM = 512

# TCP header (§6.3)
MAGIC = b"MS"
VERSION = 1
HEADER_FMT = "!2sBBII"  # magic, version, type, req_id, body_len
HEADER_SIZE = 12
assert struct.calcsize(HEADER_FMT) == HEADER_SIZE
MAX_BODY = 1024 * 1024
MAX_REQ_ID = 0xFFFFFFFF

# Timers, seconds (§6.2, §6.5, §6.7, D13)
ANNOUNCE_INTERVAL = 5
PEER_EXPIRY = 15
ANNOUNCE_MAX_SKEW = 30
HANDSHAKE_TIMEOUT = 10
PING_INTERVAL = 10
MAX_MISSED_PONGS = 3
SCAN_INTERVAL = 2

# Authentication (§6.5)
NONCE_BYTES = 16

# Hex string lengths in message bodies (§6.2, §6.4, §7.1)
PEER_ID_HEX_LEN = 8  # §6.2: peer_id is 8 hex characters
SHA256_HEX_LEN = 64  # SHA-256 digest (32 bytes) as hex: chunk/file hashes, MACs, tags

# Transfer (§7.3)
MAX_OUTSTANDING_PER_PEER = 8

# Content-defined chunking (D10)
CDC_MIN = 16 * 1024
CDC_AVG = 64 * 1024
CDC_MAX = 256 * 1024

# Paths (§7.3)
META_DIR = ".meshsync"

# Tombstones (§7.3, PROTOCOL.md decision (e)): a deleted entry has size 0, no chunks and the
# SHA-256 of empty content.
EMPTY_FILE_HASH = hashlib.sha256(b"").hexdigest()


class MsgType(IntEnum):
    """TCP message types (§6.4)."""

    HELLO = 0x01
    HELLO_ACK = 0x02
    AUTH = 0x03
    AUTH_OK = 0x04
    INDEX_REQ = 0x10
    INDEX = 0x11
    NOTIFY = 0x12
    CHUNK_REQ = 0x20
    CHUNK_DATA = 0x21
    PING = 0x30
    PONG = 0x31
    ERROR = 0x7E
    BYE = 0x7F


class ErrorCode(IntEnum):
    """ERROR body codes (§6.8)."""

    BAD_HEADER = 0x01
    BAD_VERSION = 0x02
    ILLEGAL_STATE = 0x03
    AUTH_FAILED = 0x04
    TOO_LARGE = 0x05
    CHUNK_NOT_FOUND = 0x10
    CHUNK_CORRUPT = 0x11
    BUSY = 0x20
    INTERNAL = 0x7F


FATAL_ERRORS: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.BAD_HEADER,
        ErrorCode.BAD_VERSION,
        ErrorCode.ILLEGAL_STATE,
        ErrorCode.AUTH_FAILED,
        ErrorCode.TOO_LARGE,
    }
)
