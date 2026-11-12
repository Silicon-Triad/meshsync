"""§10 failure scenarios between real `meshsync run` processes on localhost (R7, issue #63).

Each test checks the expected result from PROJECT_CONTEXT.md §10 and the log line that is the
evidence for the report. The evidence lines (logger, level, text) are listed in README.md.

Not here: concurrent edits and offline/rejoin (end-to-end issue #62), corrupt chunk (blocked on
the test-flag decision in #63), 5 % loss (needs root + netem, run by hand with scripts/netem.sh).
"""

import hashlib
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from mesh import Peer, wait_until, write_secret

from meshsync.net import discovery
from meshsync.net.auth import SECRET_BYTES, PeerIdentity
from meshsync.protocol.constants import ErrorCode

pytestmark = pytest.mark.slow

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def key() -> bytes:
    return secrets.token_bytes(SECRET_BYTES)


@pytest.fixture
def mesh(tmp_path: Path) -> Iterator[list[Peer]]:
    """Collects the peers a test starts and stops them all afterwards."""
    peers: list[Peer] = []
    yield peers
    for p in peers:
        p.stop()


def new_peer(mesh: list[Peer], root: Path, name: str, key: bytes, **opts: object) -> Peer:
    secret = write_secret(root / f"{name}.secret", key)
    peer = Peer(root, name, secret, **opts)
    mesh.append(peer)
    return peer


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(1 << 20):
            h.update(block)
    return h.hexdigest()


# ---------------------------------------------------------------- peer killed mid-transfer


def test_peer_killed_mid_transfer_completes_from_other_peer(
    tmp_path: Path, key: bytes, mesh: list[Peer]
) -> None:
    """§10: outstanding chunks reassigned; file completes from the other peer (§7.3, D12)."""
    data = tmp_path / "testdata"
    subprocess.run(
        [sys.executable, str(REPO / "scripts" / "gen_testdata.py"), str(data), "--sets", "A"],
        check=True,
        capture_output=True,
    )
    big = data / "large" / "large.bin"  # set A (issue #25)
    want = sha256_file(big)

    a = new_peer(mesh, tmp_path, "alice", key)
    b = new_peer(mesh, tmp_path, "bob", key)
    c = new_peer(mesh, tmp_path, "carl", key)
    shutil.copy(big, a.dir / "large.bin")
    a.start()
    b.start([a])  # bob gets the file from alice first, so both can serve it
    assert wait_until(lambda: (b.dir / "large.bin").exists(), 120), b.log()
    assert sha256_file(b.dir / "large.bin") == want

    c.start([a, b])
    # Kill alice as soon as carl fetches from both peers (scheduler INFO line).
    assert c.wait_log("fetching large.bin:", 60), c.log()
    assert wait_until(lambda: "from 2 peers" in c.log(), 10, step=0.005), c.log()
    time.sleep(0.5)  # let requests be in flight to both peers (set A takes seconds here)
    a.kill()

    assert wait_until(
        lambda: (c.dir / "large.bin").exists() and sha256_file(c.dir / "large.bin") == want, 120
    ), c.log()
    log = c.log()
    assert "peer down: alice" in log, log
    # Reassignment evidence (scheduler.py): a reply lost with the connection (WARNING), or a
    # CHUNK_REQ that could not be sent any more (INFO). Either way the chunk goes to bob.
    assert "lost during fetch" in log or re.search(r"CHUNK_REQ to [0-9A-F]{8} failed", log), log
    assert "fetched all chunks of large.bin" in log


# ---------------------------------------------------------------- wrong secret


def test_wrong_secret_error_auth_failed_and_closed(
    tmp_path: Path, key: bytes, mesh: list[Peer]
) -> None:
    """§10: ERROR 0x04 (AUTH_FAILED, §6.8), connection closed, no peer up (§6.5, D4)."""
    a = new_peer(mesh, tmp_path, "alice", key).start()
    b = new_peer(mesh, tmp_path, "bob", secrets.token_bytes(SECRET_BYTES)).start([a])

    # bob dials, so bob checks mac_r first, sends ERROR AUTH_FAILED and closes.
    assert b.wait_log("initiator handshake failed: AUTH_FAILED: bad mac_r"), b.log()
    assert a.wait_log(f"responder handshake failed: {ErrorCode.AUTH_FAILED.name}"), a.log()
    assert a.wait_log("handshake from 127.0.0.1 failed"), a.log()
    assert int(ErrorCode.AUTH_FAILED) == 0x04
    assert "peer up" not in a.log() + b.log()


# ---------------------------------------------------------------- spoofed / replayed ANNOUNCE


def _send_udp(port: int, data: bytes) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.sendto(data, ("127.0.0.1", port))


def test_spoofed_and_replayed_announce_dropped(
    tmp_path: Path, key: bytes, mesh: list[Peer]
) -> None:
    """§10: dropped (bad tag / stale ts); replay of an accepted ts dropped too (decision (b))."""
    a = new_peer(mesh, tmp_path, "alice", key).start()
    now = int(time.time())

    spoof = discovery.build_announce(
        secrets.token_bytes(SECRET_BYTES), PeerIdentity("BADBAD00", "mallory"), 1, 0, now
    )
    _send_udp(a.udp_port, spoof)
    assert a.wait_log("drop BADBAD00 from 127.0.0.1: bad tag"), a.log()

    # Signed with the right key but too old: ANNOUNCE_MAX_SKEW (§6.2) is exceeded by 100x.
    old = now - 100 * discovery.ANNOUNCE_MAX_SKEW
    stale = discovery.build_announce(key, PeerIdentity("01D00000", "oldie"), 1, 0, old)
    _send_udp(a.udp_port, stale)
    assert a.wait_log(f"drop 01D00000 from 127.0.0.1: stale ts {old}"), a.log()

    good = discovery.build_announce(key, PeerIdentity("CAFE0000", "eve"), 1, 0, now)
    _send_udp(a.udp_port, good)
    time.sleep(0.5)  # let the first copy be accepted before the replay arrives
    _send_udp(a.udp_port, good)
    assert a.wait_log(f"drop CAFE0000 from 127.0.0.1: replay ts {now}"), a.log()


# ---------------------------------------------------------------- untrusted cert / mode mismatch


def test_untrusted_cert_tls_alert_and_closed(
    tmp_path: Path, key: bytes, mesh: list[Peer], certs: Path, rogue: Path
) -> None:
    """§10: a cert from another CA gets a TLS alert; connection closed (§6.6, D9)."""
    a = new_peer(mesh, tmp_path, "alice", key, cert_dir=certs).start()
    m = new_peer(mesh, tmp_path, "mallory", key, cert_dir=rogue).start([a])

    assert m.wait_log("TLS handshake failed (client side)"), m.log()
    assert a.wait_log("TLS handshake failed (server side)"), a.log()
    assert "CERTIFICATE_VERIFY_FAILED" in m.log() + a.log()
    assert "peer up" not in a.log() + m.log()


def test_tls_plain_mode_mismatch_closed(
    tmp_path: Path, key: bytes, mesh: list[Peer], certs: Path
) -> None:
    """§10: --plain dialling a --tls peer fails the TLS handshake; connection closed (§6.6)."""
    a = new_peer(mesh, tmp_path, "alice", key, cert_dir=certs).start()
    b = new_peer(mesh, tmp_path, "bob", key).start([a])  # plain

    assert a.wait_log("TLS handshake failed (server side)"), a.log()
    assert "peer up" not in a.log() + b.log()
    assert b.proc is not None and b.proc.poll() is None  # bob keeps running and retrying
