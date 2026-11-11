"""Tests for scripts/gen_testdata.py (issue #19; data set decided in #25)."""

import hashlib
import importlib.util
import struct
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "gen_testdata.py"
_spec = importlib.util.spec_from_file_location("gen_testdata", _PATH)
gt = importlib.util.module_from_spec(_spec)
sys.modules["gen_testdata"] = gt  # dataclasses need the module registered
_spec.loader.exec_module(gt)

SMALL = gt.Spec(
    large=10_000,
    sweep=(100, 5_000),
    small_count=3,
    small_size=64,
    edit=50,
    text=3_000,
    paragraph=200,
    delete_count=2,
    delete_size=32,
)


def _read(out: Path, rel: str) -> bytes:
    return (out / rel).read_bytes()


def test_defaults_match_issue_25() -> None:
    s = gt.Spec()
    assert s.large == 100 * 1024 * 1024
    assert s.sweep == (64 * 1024, 1024 * 1024, 10 * 1024 * 1024)
    assert (s.small_count, s.small_size) == (1000, 4096)
    assert s.edit == 1024 and s.text == 10 * 1024 * 1024
    assert (s.delete_count, s.delete_size) == (10, 4096)


def test_stream_is_sha256_counter_blocks() -> None:
    fs = b"seed"
    expected = b"".join(hashlib.sha256(fs + struct.pack(">Q", c)).digest() for c in range(4))
    assert b"".join(gt.stream(fs, 0, 128)) == expected


@pytest.mark.parametrize("start", [0, 1, 31, 32, 33, 100])
def test_stream_offset_matches_full_stream(start: int) -> None:
    full = b"".join(gt.stream(b"x", 0, 300))
    assert b"".join(gt.stream(b"x", start, 150)) == full[start : start + 150]


def test_stream_spans_write_boundary() -> None:
    n = gt._WRITE + 7
    data = b"".join(gt.stream(b"y", 5, n))
    assert len(data) == n
    assert data[-7:] == b"".join(gt.stream(b"y", 5 + n - 7, 7))


def test_file_seed_is_length_prefixed_no_collision() -> None:
    # #25: set "A" + name "1x" must differ from set "A1" + name "x".
    assert gt.file_seed(0, "A", "1x") != gt.file_seed(0, "A1", "x")
    assert gt.file_seed(1, "A", "x") != gt.file_seed(2, "A", "x")


def test_generate_sizes_and_layout(tmp_path: Path) -> None:
    gt.generate(tmp_path, 7, spec=SMALL)
    assert len(_read(tmp_path, "large/large.bin")) == 10_000
    assert len(_read(tmp_path, "sizes/0k.bin")) == 100
    assert len(_read(tmp_path, "small/f0002.bin")) == 64
    assert len(_read(tmp_path, "text/text.txt")) == 3_000
    assert len(_read(tmp_path, "deletes/d01.bin")) == 32
    assert not (tmp_path / "small/f0003.bin").exists()


def test_same_seed_same_bytes_other_seed_differs(tmp_path: Path) -> None:
    a = gt.generate(tmp_path / "a", 7, spec=SMALL)
    b = gt.generate(tmp_path / "b", 7, spec=SMALL)
    c = gt.generate(tmp_path / "c", 8, spec=SMALL)
    assert a == b
    assert set(a).isdisjoint(c)


def test_files_within_a_run_are_distinct(tmp_path: Path) -> None:
    lines = gt.generate(tmp_path, 7, spec=SMALL)
    hashes = [line.split()[0] for line in lines]
    assert len(set(hashes)) == len(hashes)


def test_edit_variants_relate_to_large(tmp_path: Path) -> None:
    gt.generate(tmp_path, 7, spec=SMALL)
    a = _read(tmp_path, "large/large.bin")
    ow = _read(tmp_path, "edits/overwrite.bin")
    ins = _read(tmp_path, "edits/insert.bin")
    app = _read(tmp_path, "edits/append.bin")
    mid, e = 5_000, 50
    assert len(ow) == len(a)
    assert ow[:mid] == a[:mid] and ow[mid + e :] == a[mid + e :]
    assert ow[mid : mid + e] != a[mid : mid + e]
    assert len(ins) == len(a) + e and ins[e:] == a
    assert len(app) == len(a) + e and app[: len(a)] == a


def test_text_is_seeded_repeated_ascii(tmp_path: Path) -> None:
    gt.generate(tmp_path / "s1", 1, "E", SMALL)
    gt.generate(tmp_path / "s2", 2, "E", SMALL)
    t1 = _read(tmp_path / "s1", "text/text.txt")
    t2 = _read(tmp_path / "s2", "text/text.txt")
    para = t1[:200]
    assert para.endswith(b"\n") and set(para) <= set(b"abcdefghijklmnopqrstuvwxyz \n")
    assert t1 == (para * 15)[:3_000]
    assert t1 != t2  # #25: the seed changes the paragraph


def test_manifest_matches_files(tmp_path: Path) -> None:
    gt.generate(tmp_path, 3, spec=SMALL)
    lines = (tmp_path / gt.MANIFEST).read_text().splitlines()
    assert len(lines) == 1 + 2 + 3 + 3 + 1 + 2
    for line in lines:
        digest, rel = line.split("  ", 1)
        assert hashlib.sha256(_read(tmp_path, rel)).hexdigest() == digest


def test_sets_filter_and_d_without_a(tmp_path: Path) -> None:
    gt.generate(tmp_path / "full", 3, spec=SMALL)
    lines = gt.generate(tmp_path / "d", 3, "D", SMALL)
    assert [line.split("  ")[1] for line in lines] == [
        "edits/append.bin",
        "edits/insert.bin",
        "edits/overwrite.bin",
    ]
    assert not (tmp_path / "d/large").exists()
    assert _read(tmp_path / "d", "edits/insert.bin") == _read(tmp_path / "full", "edits/insert.bin")


def test_cli_rejects_bad_sets_and_seed(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        gt.main([str(tmp_path), "--sets", "AZ"])
    with pytest.raises(SystemExit):
        gt.main([str(tmp_path), "--seed", "-1"])


def test_sets_parser_accepts_commas() -> None:
    assert gt._sets("a, d") == "AD"
