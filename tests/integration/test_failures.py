"""§10 failure scenarios between real `meshsync run` processes on 127.0.0.1 (R7, #63).

Each test checks the expected result from PROJECT_CONTEXT.md §10 and the log line that is the
evidence for the report; README.md lists the lines. Harness: mesh.py (#62).

Not here: concurrent edits and offline/rejoin (test_sync.py, #62), corrupt chunk (approach still
being agreed on #63), 5 % loss (needs root + netem, run by hand with scripts/netem.sh).
"""

import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from mesh import Mesh, sha256

from meshsync.net import discovery
from meshsync.net.auth import PeerIdentity, load_secret
from meshsync.protocol.constants import ANNOUNCE_MAX_SKEW, ErrorCode

pytestmark = [pytest.mark.integration, pytest.mark.slow]

REPO = Path(__file__).resolve().parents[2]
NO_LINK = "peer up:"  # manager.py INFO line; must never appear when the link is refused


# ---------------------------------------------------------------- peer killed mid-transfer


def test_peer_killed_mid_transfer_completes_from_other_peer(make_mesh, tmp_path: Path) -> None:
    """§10: outstanding chunks reassigned; file completes from the other peer (§7.3, D12)."""
    data = tmp_path / "testdata"
    subprocess.run(
        [sys.executable, str(REPO / "scripts" / "gen_testdata.py"), str(data), "--sets", "A"],
        check=True,
        capture_output=True,
    )
    big = data / "large" / "large.bin"  # set A (issue #25)
    want = sha256(big)

    mesh: Mesh = make_mesh(["alice", "bob", "carl"])
    alice, bob, carl = mesh["alice"], mesh["bob"], mesh["carl"]
    alice.path("large.bin").write_bytes(big.read_bytes())
    alice.start()
    bob.start()  # bob copies the file from alice first, so both can serve it
    mesh.wait(lambda: bob.path("large.bin").is_file() and sha256(bob.path("large.bin")) == want,
              "bob has large.bin")  # fmt: skip

    carl.start()
    # Kill alice on carl's first CHUNK_DATA from her (metrics DEBUG line, §11), so requests to
    # alice are still outstanding when she dies.
    carl.wait_log("recv peer=alice type=CHUNK_DATA", "carl's first chunk from alice")
    alice.kill()
    if "fetched all chunks of large.bin" in carl.log_text():
        pytest.skip("carl finished the whole transfer before alice was killed; nothing reassigned")

    mesh.wait(lambda: carl.path("large.bin").is_file() and sha256(carl.path("large.bin")) == want,
              "carl has large.bin from bob")  # fmt: skip
    log = carl.log_text()
    assert "peer down: alice" in log, log[-4000:]
    # Reassignment evidence (scheduler.py): a reply lost with the connection (WARNING), or a
    # CHUNK_REQ that could not be sent any more (INFO). Either way the chunk goes to bob.
    assert "lost during fetch" in log or re.search(r"CHUNK_REQ to [0-9A-F]{8} failed", log), log[
        -4000:
    ]
    assert "fetched all chunks of large.bin" in log


# ---------------------------------------------------------------- wrong secret


def test_wrong_secret_error_auth_failed_and_closed(make_mesh) -> None:
    """§10: ERROR 0x04 (AUTH_FAILED, §6.8), connection closed, no peer up (§6.5, D4)."""
    assert int(ErrorCode.AUTH_FAILED) == 0x04
    mesh: Mesh = make_mesh(["alice", "bob"], own_secret=frozenset({"bob"}))
    mesh.start()
    alice, bob = mesh["alice"], mesh["bob"]
    # Both dial each other (static --peer): the dialler rejects mac_r and sends ERROR AUTH_FAILED,
    # the listener logs that ERROR and drops the socket.
    bob.wait_log("initiator handshake failed: AUTH_FAILED: bad mac_r")
    alice.wait_log("responder handshake failed: AUTH_FAILED: peer sent ERROR")
    alice.wait_log("handshake from 127.0.0.1 failed")
    assert NO_LINK not in alice.log_text() + bob.log_text()


# ---------------------------------------------------------------- spoofed / replayed ANNOUNCE


def _send_udp(port: int, data: bytes) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.sendto(data, ("127.0.0.1", port))


def test_spoofed_and_replayed_announce_dropped(make_mesh) -> None:
    """§10: dropped (bad tag / stale ts); replay of an accepted ts dropped too (decision (b))."""
    mesh: Mesh = make_mesh(["alice"])
    mesh.start()
    alice = mesh["alice"]
    alice.wait_log("peer manager started")
    key = load_secret(mesh.secret)
    wrong = bytes(b ^ 0xFF for b in key)
    now = int(time.time())

    spoof = discovery.build_announce(wrong, PeerIdentity("BADBAD00", "mallory"), 1, 0, now)
    _send_udp(mesh.udp_port, spoof)
    alice.wait_log("drop BADBAD00 from 127.0.0.1: bad tag")

    old = now - 2 * ANNOUNCE_MAX_SKEW  # right key, ts outside the §6.2 skew window
    stale = discovery.build_announce(key, PeerIdentity("01D00000", "oldie"), 1, 0, old)
    _send_udp(mesh.udp_port, stale)
    alice.wait_log(f"drop 01D00000 from 127.0.0.1: stale ts {old}")

    good = discovery.build_announce(key, PeerIdentity("CAFE0000", "eve"), 1, 0, now)
    _send_udp(mesh.udp_port, good)
    _send_udp(mesh.udp_port, good)  # same datagram again: ts not newer than the accepted one
    alice.wait_log(f"drop CAFE0000 from 127.0.0.1: replay ts {now}")


# ---------------------------------------------------------------- untrusted cert / mode mismatch


def test_untrusted_cert_tls_alert_and_closed(make_mesh, rogue: Path) -> None:
    """§10: a cert from another CA gets a TLS alert; connection closed (§6.6, D9)."""
    mesh: Mesh = make_mesh(["alice", "mallory"], tls=True, cert_dir={"mallory": rogue})
    mesh.start()
    alice, mallory = mesh["alice"], mesh["mallory"]
    alice.wait_log("TLS handshake failed (server side)")
    mallory.wait_log("TLS handshake failed")
    assert "CERTIFICATE_VERIFY_FAILED" in alice.log_text() + mallory.log_text()
    assert NO_LINK not in alice.log_text() + mallory.log_text()


def test_tls_plain_mode_mismatch_closed(make_mesh) -> None:
    """§10: a --plain peer and a --tls peer never link; TLS handshake fails, closed (§6.6)."""
    mesh: Mesh = make_mesh(["alice", "bob"], tls=True, plain=frozenset({"bob"}))
    mesh.start()
    alice, bob = mesh["alice"], mesh["bob"]
    alice.wait_log("TLS handshake failed (server side)")  # bob's plain HELLO hits TLS
    alice.wait_log("TLS handshake failed (client side)")  # alice's TLS hello hits plain bob
    assert NO_LINK not in alice.log_text() + bob.log_text()
