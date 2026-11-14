#!/usr/bin/env python3
"""Replace MAC addresses in a pcapng capture before it is committed (#68, privacy).

Usage: scripts/measure/anon_mac.py IN.pcapng OUT.pcapng

Every unicast MAC in the Ethernet header of each packet (link type 1, which is what tshark
records on a Linux Wi-Fi interface in managed mode) is replaced by a locally administered fake,
02:00:00:00:00:01, :02, ... in order of first appearance, the same fake for the same device in
every packet. Broadcast and multicast addresses (first octet odd) are kept. Nothing else changes:
IPs, ports, timestamps, captured/original lengths and payload bytes are copied as they are.
The mapping is printed to stderr for the person running it, not stored. Standard library only.
Packets with another link type are copied unchanged, with a warning.
Owner: Huzaifa Hanif (CS-24039); written for #68 by Mujtaba Jawaid Rao.
"""

from __future__ import annotations

import logging
import struct
import sys
from pathlib import Path

log = logging.getLogger("anon_mac")

SHB, IDB, SPB, EPB = 0x0A0D0D0A, 0x00000001, 0x00000003, 0x00000006
BYTE_ORDER_MAGIC = 0x1A2B3C4D
LINKTYPE_ETHERNET = 1


def fmt_mac(mac: bytes) -> str:
    return ":".join(f"{b:02x}" for b in mac)


class MacMap:
    """Real MAC -> stable fake MAC; broadcast/multicast addresses pass through."""

    def __init__(self) -> None:
        self.fakes: dict[bytes, bytes] = {}

    def __call__(self, mac: bytes) -> bytes:
        if mac[0] & 1:  # group bit: broadcast or multicast, not a device
            return mac
        if mac not in self.fakes:
            n = len(self.fakes) + 1
            self.fakes[mac] = bytes([0x02, 0, 0, 0, (n >> 8) & 0xFF, n & 0xFF])
        return self.fakes[mac]


def rewrite_ethernet(frame: bytearray, macs: MacMap) -> None:
    if len(frame) >= 12:
        frame[0:6] = macs(bytes(frame[0:6]))
        frame[6:12] = macs(bytes(frame[6:12]))


def anonymize(data: bytes, macs: MacMap) -> bytes:
    """The pcapng file `data` with every Ethernet MAC replaced."""
    out = bytearray()
    pos = 0
    endian = "<"
    linktypes: list[int] = []
    warned = False
    while pos < len(data):
        if len(data) - pos < 12:
            raise ValueError(f"truncated block at offset {pos}")
        btype = struct.unpack_from("<I", data, pos)[0]
        if btype == SHB:  # section header: byte order and interface numbering restart here
            magic = struct.unpack_from("<I", data, pos + 8)[0]
            endian = "<" if magic == BYTE_ORDER_MAGIC else ">"
            if struct.unpack_from(endian + "I", data, pos + 8)[0] != BYTE_ORDER_MAGIC:
                raise ValueError("bad section header: not a pcapng file")
            linktypes = []
        btype, blen = struct.unpack_from(endian + "II", data, pos)
        if blen < 12 or blen % 4 or pos + blen > len(data):
            raise ValueError(f"bad block length {blen} at offset {pos}")
        block = bytearray(data[pos : pos + blen])
        if btype == IDB:
            linktypes.append(struct.unpack_from(endian + "H", block, 8)[0])
        elif btype in (EPB, SPB):
            if btype == EPB:
                iface = struct.unpack_from(endian + "I", block, 8)[0]
                caplen = struct.unpack_from(endian + "I", block, 20)[0]
                start = 28
            else:  # simple packet block: interface 0, captured length bounded by the block
                iface = 0
                caplen = blen - 16
                start = 12
            lt = linktypes[iface] if iface < len(linktypes) else None
            if lt == LINKTYPE_ETHERNET:
                frame = bytearray(block[start : start + caplen])
                rewrite_ethernet(frame, macs)
                block[start : start + caplen] = frame
            elif not warned:
                log.warning("link type %s is not Ethernet: packets copied unchanged", lt)
                warned = True
        out += block
        pos += blen
    return bytes(out)


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if len(argv) != 2:
        print("usage: anon_mac.py IN.pcapng OUT.pcapng", file=sys.stderr)
        return 2
    src, dst = Path(argv[0]), Path(argv[1])
    macs = MacMap()
    try:
        out = anonymize(src.read_bytes(), macs)
    except ValueError as e:
        log.error("%s: %s", src, e)
        return 1
    dst.write_bytes(out)
    for real, fake in macs.fakes.items():
        log.info("%s -> %s", fmt_mac(real), fmt_mac(fake))
    log.info("wrote %s (%d MAC address(es) replaced)", dst, len(macs.fakes))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
