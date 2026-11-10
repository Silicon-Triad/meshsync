"""File chunking: content-defined (Gear/FastCDC) and fixed-size.

Owner: Mujtaba Jawaid Rao (CS-24047)
Contract (PROJECT_CONTEXT.md §8.3):
    chunk_file(path, mode) yields (offset, length, sha256) (D10).
    CDC sizes CDC_MIN / CDC_AVG / CDC_MAX; fixed mode for comparison (--chunking fixed).

CDC (D10, PROTOCOL.md §9.7) is FastCDC-style: a Gear rolling hash, no cut before CDC_MIN, a
stricter mask before CDC_AVG and a looser one after it (normalized chunking), forced cut at
CDC_MAX. Fixed mode cuts every CDC_AVG bytes so both modes have the same average chunk size.
Peers need not agree on the algorithm: chunks are only ever named by their SHA-256 (§9.7).
"""
# FROZEN INTERFACE (AGENTS.md §6): changing or adding to the public interface in this file
# needs an issue and approval from all three members first (R-6.2).

import hashlib
import logging
import os
from collections.abc import Iterator

from meshsync.protocol.constants import CDC_AVG, CDC_MAX, CDC_MIN

log = logging.getLogger(__name__)

_MASK64 = (1 << 64) - 1
# Gear table: 256 pseudo-random 64-bit values, derived deterministically from SHA-256 so the
# table is reproducible and needs no hard-coded constants.
_GEAR = tuple(int.from_bytes(hashlib.sha256(bytes([i])).digest()[:8], "big") for i in range(256))

_AVG_BITS = CDC_AVG.bit_length() - 1  # log2(CDC_AVG)
# Normalization level 2 from the FastCDC paper: +/-2 mask bits around log2(avg). Masks test the
# high bits of the hash, which depend on the most recent ~64 bytes of the window.
_MASK_S = ((1 << (_AVG_BITS + 2)) - 1) << (64 - (_AVG_BITS + 2))
_MASK_L = ((1 << (_AVG_BITS - 2)) - 1) << (64 - (_AVG_BITS - 2))

_READ_SIZE = 4 * CDC_MAX


def cut_point(data: bytes | bytearray | memoryview, start: int = 0) -> int:
    """Length of the CDC chunk beginning at data[start:] (D10). Takes at most CDC_MAX bytes."""
    n = min(len(data) - start, CDC_MAX)
    if n <= CDC_MIN:
        return n
    gear, ms, ml, m64 = _GEAR, _MASK_S, _MASK_L, _MASK64
    h = 0
    i = start + CDC_MIN
    normal = start + min(CDC_AVG, n)
    end = start + n
    while i < normal:
        h = ((h << 1) + gear[data[i]]) & m64
        if not h & ms:
            return i - start + 1
        i += 1
    while i < end:
        h = ((h << 1) + gear[data[i]]) & m64
        if not h & ml:
            return i - start + 1
        i += 1
    return n


def chunk_file(path: str | os.PathLike[str], mode: str = "cdc") -> Iterator[tuple[int, int, str]]:
    """Yield (offset, length, sha256 hex) for each chunk of the file (D10, §8.3).

    mode is "cdc" or "fixed" (config.CHUNKING_MODES). An empty file yields nothing.
    """
    if mode not in ("cdc", "fixed"):
        raise ValueError(f"unknown chunking mode: {mode!r}")
    offset = 0
    buf = bytearray()
    eof = False
    with open(path, "rb") as f:
        while True:
            # Keep at least CDC_MAX bytes buffered so every cut sees a full window.
            while not eof and len(buf) < CDC_MAX:
                block = f.read(_READ_SIZE)
                if not block:
                    eof = True
                else:
                    buf += block
            if not buf:
                return
            pos = 0
            while len(buf) - pos >= CDC_MAX or (eof and pos < len(buf)):
                if mode == "fixed":
                    length = min(CDC_AVG, len(buf) - pos)
                else:
                    length = cut_point(buf, pos)
                digest = hashlib.sha256(memoryview(buf)[pos : pos + length]).hexdigest()
                yield offset, length, digest
                offset += length
                pos += length
            del buf[:pos]
