#!/usr/bin/env python3
"""Generate the measurement test data set (PROJECT_CONTEXT.md §11; decided in issue #25, v2).

Usage: scripts/gen_testdata.py OUT_DIR [--seed N] [--sets A,B,C,D,E,F]

Sets (issue #25 v2; sizes in MiB/KiB, "100 MiB" stands for §11's "100 MB"):
  A  large/large.bin           1 x 100 MiB random    §11 sync time, throughput, chunk-size runs
  B  sizes/*.bin               64 KiB, 1 MiB, 10 MiB random; the 100 MiB point reuses A
  C  small/fNNNN.bin           1,000 x 4 KiB random  per-file overhead
  D  edits/{overwrite,insert,append}.bin
                               A with 1 KiB overwritten mid-file / inserted at 0 / appended
  E  text/text.txt             10 MiB: one seeded ASCII paragraph repeated (CDC dedup, D10)
  F  deletes/dNN.bin           10 x 4 KiB random; §10 demos delete these (generator only creates)

Bytes are SHA-256(file_seed || counter) blocks (hashlib), so the output is identical on every
Python version. Writes OUT_DIR/MANIFEST.sha256 (sha256sum format), merged across subset
runs. Never commit the output.
Owner: Huzaifa Hanif (CS-24039); written by Mujtaba Jawaid Rao (CS-24047). Issue #19.
"""

from __future__ import annotations

import argparse
import hashlib
import struct
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

KIB = 1024
MIB = 1024 * KIB
_BLOCK = hashlib.sha256().digest_size  # 32 bytes per counter value
_WRITE = 1 * MIB  # write granularity, keeps memory flat for 100 MiB files
_LETTERS = b"abcdefghijklmnopqrstuvwxyz"
ALL_SETS = "ABCDEF"
MANIFEST = "MANIFEST.sha256"


@dataclass(frozen=True)
class Spec:
    """Sizes of the data set. Defaults are the values approved in issue #25 (v2)."""

    large: int = 100 * MIB  # A
    sweep: tuple[int, ...] = (64 * KIB, 1 * MIB, 10 * MIB)  # B (100 MiB point = A)
    small_count: int = 1000  # C
    small_size: int = 4 * KIB  # C
    edit: int = 1 * KIB  # D
    text: int = 10 * MIB  # E
    paragraph: int = 1 * KIB  # E: length of the seeded paragraph
    delete_count: int = 10  # F
    delete_size: int = 4 * KIB  # F


def file_seed(seed: int, set_name: str, name: str) -> bytes:
    """Per-file seed: seed || len(set) || set || len(name) || name (length-prefixed, #25)."""
    s, n = set_name.encode(), name.encode()
    return struct.pack(">QI", seed, len(s)) + s + struct.pack(">I", len(n)) + n


def stream(fseed: bytes, start: int, length: int) -> Iterator[bytes]:
    """Yield `length` bytes of the SHA-256(fseed || uint64 counter) stream from offset `start`."""
    counter, skip = divmod(start, _BLOCK)
    remaining = length
    while remaining > 0:
        n = min(_WRITE, remaining + skip)
        blocks = -(-n // _BLOCK)
        buf = b"".join(
            hashlib.sha256(fseed + struct.pack(">Q", c)).digest()
            for c in range(counter, counter + blocks)
        )
        piece = buf[skip : skip + remaining]
        counter += blocks
        skip = 0
        remaining -= len(piece)
        yield piece


def paragraph(fseed: bytes, size: int) -> bytes:
    """Seeded ASCII paragraph of exactly `size` bytes: words of 1-8 letters, ends with newline."""
    raw = b"".join(stream(fseed, 0, size))
    out = bytearray()
    i = 0
    while len(out) < size - 1:
        wlen = raw[i % len(raw)] % 8 + 1
        for j in range(wlen):
            out.append(_LETTERS[raw[(i + 1 + j) % len(raw)] % 26])
        out.append(ord(" "))
        i += wlen + 1
    return bytes(out[: size - 1]) + b"\n"


def _repeat(unit: bytes, length: int) -> Iterator[bytes]:
    full, rest = divmod(length, len(unit))
    for _ in range(full):
        yield unit
    if rest:
        yield unit[:rest]


Source = Callable[[], Iterator[bytes]]


def plan(seed: int, spec: Spec, sets: str) -> list[tuple[str, Source]]:
    """Return (relative path, byte source) for every file in the chosen sets."""
    files: list[tuple[str, Source]] = []

    def rand(set_name: str, rel: str, size: int) -> None:
        fs = file_seed(seed, set_name, rel)
        files.append((rel, lambda: stream(fs, 0, size)))

    a_seed = file_seed(seed, "A", "large/large.bin")
    if "A" in sets:
        rand("A", "large/large.bin", spec.large)
    if "B" in sets:
        for size in spec.sweep:
            rand("B", f"sizes/{size // KIB}k.bin", size)
    if "C" in sets:
        for i in range(spec.small_count):
            rand("C", f"small/f{i:04d}.bin", spec.small_size)
    if "D" in sets:
        mid = spec.large // 2

        def edited(rel: str, kind: str) -> Source:
            e = b"".join(stream(file_seed(seed, "D", rel), 0, spec.edit))

            def gen() -> Iterator[bytes]:
                if kind == "insert":
                    yield e
                    yield from stream(a_seed, 0, spec.large)
                elif kind == "append":
                    yield from stream(a_seed, 0, spec.large)
                    yield e
                else:  # overwrite: same length as A
                    yield from stream(a_seed, 0, mid)
                    yield e
                    yield from stream(a_seed, mid + spec.edit, spec.large - mid - spec.edit)

            return gen

        for kind in ("overwrite", "insert", "append"):
            rel = f"edits/{kind}.bin"
            files.append((rel, edited(rel, kind)))
    if "E" in sets:
        para = paragraph(file_seed(seed, "E", "text/text.txt"), spec.paragraph)
        files.append(("text/text.txt", lambda: _repeat(para, spec.text)))
    if "F" in sets:
        for i in range(spec.delete_count):
            rand("F", f"deletes/d{i:02d}.bin", spec.delete_size)
    return files


def generate(out: Path, seed: int, sets: str = ALL_SETS, spec: Spec | None = None) -> list[str]:
    """Write the chosen sets under `out` plus MANIFEST.sha256; return the manifest lines."""
    lines = []
    for rel, source in plan(seed, spec or Spec(), sets):
        path = out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        h = hashlib.sha256()
        with path.open("wb") as f:
            for piece in source():
                h.update(piece)
                f.write(piece)
        lines.append(f"{h.hexdigest()}  {rel}")
    lines.sort(key=lambda line: line.split("  ", 1)[1])
    # Merge with an existing manifest so subset runs (e.g. A now, the rest later) keep the
    # lines of sets generated earlier; this run's files replace their old lines (#58 review).
    manifest = out / MANIFEST
    merged: dict[str, str] = {}
    if manifest.exists():
        for line in manifest.read_text().splitlines():
            if "  " in line:
                merged[line.split("  ", 1)[1]] = line
    merged.update((line.split("  ", 1)[1], line) for line in lines)
    manifest.write_text("".join(f"{merged[rel]}\n" for rel in sorted(merged)))
    return lines


def _sets(value: str) -> str:
    sets = "".join(value.replace(",", "").upper().split())
    bad = set(sets) - set(ALL_SETS)
    if not sets or bad:
        raise argparse.ArgumentTypeError(f"sets must be letters from {ALL_SETS}, got {value!r}")
    return sets


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("out_dir", type=Path)
    p.add_argument("--seed", type=int, default=0, help="uint64 seed (default 0)")
    p.add_argument("--sets", type=_sets, default=ALL_SETS, help="e.g. A,D (default all)")
    args = p.parse_args(argv)
    if not 0 <= args.seed < 2**64:
        p.error("--seed must fit in uint64")
    lines = generate(args.out_dir, args.seed, args.sets)
    print(f"wrote {len(lines)} files and {MANIFEST} to {args.out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
