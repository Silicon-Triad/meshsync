"""Tests for scripts/measure/anon_mac.py (#68): pcapng files built in the test, no network."""

import importlib.util
import struct
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "measure" / "anon_mac.py"
_spec = importlib.util.spec_from_file_location("anon_mac", _PATH)
am = importlib.util.module_from_spec(_spec)
sys.modules["anon_mac"] = am
_spec.loader.exec_module(am)

A = bytes.fromhex("a86daa111395")  # made-up device MACs
B = bytes.fromhex("24ec99d89a01")
BCAST = b"\xff" * 6


def block(btype: int, body: bytes, e: str = "<") -> bytes:
    body += b"\x00" * (-len(body) % 4)
    n = 12 + len(body)
    return struct.pack(e + "II", btype, n) + body + struct.pack(e + "I", n)


def shb(e: str = "<") -> bytes:
    return block(am.SHB, struct.pack(e + "IHHq", am.BYTE_ORDER_MAGIC, 1, 0, -1), e)


def idb(linktype: int = 1, e: str = "<") -> bytes:
    return block(am.IDB, struct.pack(e + "HHI", linktype, 0, 256), e)


def epb(frame: bytes, iface: int = 0, origlen: int | None = None, e: str = "<") -> bytes:
    hdr = struct.pack(e + "IIIII", iface, 1, 2, len(frame), origlen or len(frame))
    return block(am.EPB, hdr + frame, e)


def eth(dst: bytes, src: bytes, payload: bytes = b"\x08\x00IPv4-and-meshsync-bytes") -> bytes:
    return dst + src + payload


def frames(data: bytes, e: str = "<") -> list[bytes]:
    out, pos = [], 0
    while pos < len(data):
        btype, blen = struct.unpack_from(e + "II", data, pos)
        if btype == am.EPB:
            caplen = struct.unpack_from(e + "I", data, pos + 20)[0]
            out.append(data[pos + 28 : pos + 28 + caplen])
        pos += blen
    return out


@pytest.mark.parametrize("e", ["<", ">"])
def test_macs_replaced_consistently_payload_untouched(e: str) -> None:
    f1, f2, f3 = eth(B, A), eth(A, B), eth(BCAST, A)
    data = shb(e) + idb(1, e) + epb(f1, e=e) + epb(f2, e=e) + epb(f3, origlen=1514, e=e)
    out = am.anonymize(data, am.MacMap())
    assert len(out) == len(data)
    g1, g2, g3 = frames(out, e)
    fake_b, fake_a = bytes.fromhex("020000000001"), bytes.fromhex("020000000002")  # B seen first
    assert (g1[:6], g1[6:12]) == (fake_b, fake_a)  # numbered in order of first appearance
    assert (g2[:6], g2[6:12]) == (fake_a, fake_b)
    assert (g3[:6], g3[6:12]) == (BCAST, fake_a)  # broadcast kept
    assert g1[12:] == f1[12:] and g3[12:] == f3[12:]  # IP/TCP/meshsync bytes unchanged
    assert A not in out and B not in out
    assert (
        struct.unpack_from(e + "I", out, len(out) - len(epb(f3, origlen=1514, e=e)) + 24)[0] == 1514
    )


def test_first_seen_order() -> None:
    m = am.MacMap()
    assert m(B) == bytes.fromhex("020000000001")
    assert m(A) == bytes.fromhex("020000000002")
    assert m(B) == bytes.fromhex("020000000001")
    assert m(bytes.fromhex("01005e000001")) == bytes.fromhex("01005e000001")  # multicast kept


def test_non_ethernet_is_copied_unchanged() -> None:
    data = shb() + idb(linktype=127) + epb(eth(B, A))  # 127 = radiotap: not handled
    assert am.anonymize(data, am.MacMap()) == data


def test_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        am.anonymize(b"\x00" * 20, am.MacMap())
    with pytest.raises(ValueError):
        am.anonymize(shb()[:-4], am.MacMap())


def test_cli(tmp_path: Path) -> None:
    src, dst = tmp_path / "in.pcapng", tmp_path / "out.pcapng"
    src.write_bytes(shb() + idb() + epb(eth(B, A)))
    assert am.main([str(src), str(dst)]) == 0
    assert A not in dst.read_bytes()
    assert am.main([str(src)]) == 2
    (tmp_path / "bad").write_bytes(b"nope")
    assert am.main([str(tmp_path / "bad"), str(dst)]) == 1
