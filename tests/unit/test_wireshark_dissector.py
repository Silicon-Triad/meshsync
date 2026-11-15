"""Tests for tools/wireshark (#82): decode the committed #68 captures with tshark.

Skipped when tshark is not installed (CI) or when running as root (Wireshark ignores Lua
scripts for root). Run locally as a normal user with tshark installed.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DECODE = ROOT / "tools" / "wireshark" / "decode.sh"
CAPS = ROOT / "data" / "captures"


def test_decode_sh_syntax_and_usage() -> None:
    subprocess.run(["bash", "-n", str(DECODE)], check=True)
    r = subprocess.run(["bash", str(DECODE)], capture_output=True, text=True)
    assert r.returncode == 2 and "usage:" in r.stderr


def counts(capture: str) -> dict[str, int]:
    r = subprocess.run(["bash", str(DECODE), str(CAPS / capture)], capture_output=True,
                       text=True, check=True)  # fmt: skip
    out = {}
    for line in r.stdout.splitlines():
        n, name = line.split()
        out[name] = int(n)
    return out


needs_tshark = pytest.mark.skipif(
    shutil.which("tshark") is None or os.geteuid() == 0,
    reason="needs tshark, run as a non-root user (Lua is disabled for root)",
)


@needs_tshark
def test_wrong_secret_capture_decodes_handshake_and_error() -> None:
    # cap2b (#68): every retry is HELLO -> HELLO_ACK -> ERROR AUTH_FAILED, 4 retries.
    c = counts("cap2b.pcapng")
    assert c["HELLO"] == c["HELLO_ACK"] == c["ERROR"] == 4
    assert c["ANNOUNCE"] > 0


@needs_tshark
def test_sync_capture_decodes_every_message_type_seen() -> None:
    c = counts("cap1.pcapng.gz")
    for name in ("HELLO", "HELLO_ACK", "AUTH", "AUTH_OK", "INDEX_REQ", "INDEX", "NOTIFY",
                 "CHUNK_REQ", "CHUNK_DATA", "PING", "PONG", "ANNOUNCE"):  # fmt: skip
        assert c.get(name, 0) > 0, name
    assert not any(k.startswith("0x") for k in c), c  # no unknown TYPE decoded
