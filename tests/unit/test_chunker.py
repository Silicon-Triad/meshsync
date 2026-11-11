"""Tests for sync/chunker.py (D10)."""

import hashlib
import random

import pytest

from meshsync.protocol.constants import CDC_AVG, CDC_MAX, CDC_MIN
from meshsync.sync.chunker import chunk_file


def _data(n: int, seed: int = 1) -> bytes:
    return random.Random(seed).randbytes(n)


def _write(tmp_path, data: bytes, name: str = "f"):
    p = tmp_path / name
    p.write_bytes(data)
    return p


@pytest.mark.parametrize("mode", ["cdc", "fixed"])
def test_chunks_cover_file_and_hash_correctly(tmp_path, mode):
    data = _data(3 * CDC_MAX + 12345)
    chunks = list(chunk_file(_write(tmp_path, data), mode))
    offset = 0
    for off, length, h in chunks:
        assert off == offset
        assert h == hashlib.sha256(data[off : off + length]).hexdigest()
        offset += length
    assert offset == len(data)


@pytest.mark.parametrize("mode", ["cdc", "fixed"])
def test_empty_file_yields_nothing(tmp_path, mode):
    assert list(chunk_file(_write(tmp_path, b""), mode)) == []


def test_small_file_is_one_chunk(tmp_path):
    data = b"hello"
    assert list(chunk_file(_write(tmp_path, data))) == [(0, 5, hashlib.sha256(data).hexdigest())]


def test_cdc_size_bounds(tmp_path):
    data = _data(40 * CDC_AVG)
    lengths = [n for _, n, _ in chunk_file(_write(tmp_path, data))]
    assert all(CDC_MIN < n <= CDC_MAX for n in lengths[:-1])
    avg = sum(lengths) / len(lengths)
    assert CDC_AVG / 2 < avg < CDC_AVG * 2


def test_cdc_max_on_uniform_data(tmp_path):
    data = bytes(3 * CDC_MAX)  # all zeros: hash never matches the mask
    lengths = [n for _, n, _ in chunk_file(_write(tmp_path, data))]
    assert lengths == [CDC_MAX] * 3


def test_fixed_chunks_are_avg_sized(tmp_path):
    data = _data(2 * CDC_AVG + 7)
    assert [n for _, n, _ in chunk_file(_write(tmp_path, data), "fixed")] == [
        CDC_AVG,
        CDC_AVG,
        7,
    ]


def test_cdc_resyncs_after_insert_near_start(tmp_path):
    # §11 experiment 2: an insert near the start re-sends few chunks with CDC, all with fixed.
    data = _data(30 * CDC_AVG)
    edited = data[:100] + b"X" + data[100:]
    a, b = _write(tmp_path, data, "a"), _write(tmp_path, edited, "b")
    cdc_old = {h for _, _, h in chunk_file(a)}
    cdc_new = [h for _, _, h in chunk_file(b)]
    assert sum(h not in cdc_old for h in cdc_new) <= 2
    fixed_old = {h for _, _, h in chunk_file(a, "fixed")}
    assert all(h not in fixed_old for _, _, h in chunk_file(b, "fixed"))


def test_deterministic(tmp_path):
    p = _write(tmp_path, _data(10 * CDC_AVG))
    assert list(chunk_file(p)) == list(chunk_file(p))


def test_unknown_mode(tmp_path):
    with pytest.raises(ValueError):
        list(chunk_file(_write(tmp_path, b"x"), "rabin"))
