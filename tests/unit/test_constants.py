import struct

from meshsync.protocol.constants import (
    FATAL_ERRORS,
    HEADER_FMT,
    HEADER_SIZE,
    ErrorCode,
    MsgType,
)


def test_header_size_is_12():
    assert HEADER_SIZE == 12
    assert struct.calcsize(HEADER_FMT) == 12


def test_no_duplicate_type_codes():
    # IntEnum silently aliases duplicate values; __members__ includes aliases.
    assert len(MsgType.__members__) == len({m.value for m in MsgType}) == 13


def test_no_duplicate_error_codes():
    assert len(ErrorCode.__members__) == len({e.value for e in ErrorCode}) == 9


def test_fatal_errors_match_spec():
    assert FATAL_ERRORS == {
        ErrorCode.BAD_HEADER,
        ErrorCode.BAD_VERSION,
        ErrorCode.ILLEGAL_STATE,
        ErrorCode.AUTH_FAILED,
        ErrorCode.TOO_LARGE,
    }
