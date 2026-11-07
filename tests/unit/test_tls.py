"""Tests for net/tls.py: TLS 1.3 mutual auth (§6.6, D9, §10 untrusted cert / mode mismatch)."""

import shutil
import socket
import ssl
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from meshsync.net import tls
from meshsync.protocol.constants import MsgType
from meshsync.protocol.framing import recv_msg, send_msg

if shutil.which("openssl") is None:
    pytest.skip("openssl not on PATH; needed by scripts/gen_certs.sh", allow_module_level=True)

GEN_CERTS = Path(__file__).resolve().parents[2] / "scripts" / "gen_certs.sh"
TIMEOUT = 5  # test-only socket timeout, so a failure never hangs the suite


def _gen(outdir: Path, *names: str) -> Path:
    subprocess.run(["bash", str(GEN_CERTS), str(outdir), *names], check=True, capture_output=True)
    return outdir


@pytest.fixture(scope="session")
def certs(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Group CA + alice + bob in a throwaway dir (never ~/.meshsync or real team certs)."""
    return _gen(tmp_path_factory.mktemp("certs"), "alice", "bob")


@pytest.fixture(scope="session")
def rogue(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A different CA with member 'mallory'; bob must not trust it."""
    return _gen(tmp_path_factory.mktemp("rogue"), "mallory")


def _run_pair(
    server_fn: Callable[[socket.socket], object], client_fn: Callable[[socket.socket], object]
) -> tuple[object, object]:
    """Run server_fn in a thread and client_fn here, on a connected socketpair.

    Each result is the function's return value or the exception it raised.
    """
    s, c = socket.socketpair()
    s.settimeout(TIMEOUT)
    c.settimeout(TIMEOUT)
    results: dict[str, object] = {}

    def call(key: str, fn: Callable[[socket.socket], object], sock: socket.socket) -> None:
        try:
            results[key] = fn(sock)
        except Exception as e:  # collected and asserted on by the test
            results[key] = e
            sock.close()  # unblock the other side

    t = threading.Thread(target=call, args=("server", server_fn, s))
    t.start()
    call("client", client_fn, c)
    t.join(TIMEOUT * 2)
    assert not t.is_alive(), "server thread hung"
    return results["server"], results["client"]


def test_contexts_are_tls13_mutual(certs: Path) -> None:
    for ctx in (tls.server_context(certs, "bob"), tls.client_context(certs, "alice")):
        assert ctx.minimum_version == ssl.TLSVersion.TLSv1_3
        assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert tls.client_context(certs, "alice").check_hostname is False


def test_alice_bob_handshake_and_framing(certs: Path) -> None:
    sctx = tls.server_context(certs, "bob")
    cctx = tls.client_context(certs, "alice")

    def server(sock: socket.socket) -> tuple[str | None, str, tuple]:
        ss = tls.wrap_server(sock, sctx)
        msg = recv_msg(ss)
        send_msg(ss, MsgType.PONG, msg[1], b"pong")
        return ss.version(), tls.peer_cert_name(ss), msg

    def client(sock: socket.socket) -> tuple[str | None, str, tuple]:
        cs = tls.wrap_client(sock, cctx)
        send_msg(cs, MsgType.PING, 7, b"ping")
        return cs.version(), tls.peer_cert_name(cs), recv_msg(cs)

    srv, cli = _run_pair(server, client)
    assert srv == ("TLSv1.3", "alice", (MsgType.PING, 7, b"ping"))
    assert cli == ("TLSv1.3", "bob", (MsgType.PONG, 7, b"pong"))


def test_rogue_ca_rejected_both_sides(certs: Path, rogue: Path) -> None:
    sctx = tls.server_context(certs, "bob")
    cctx = tls.client_context(rogue, "mallory")
    srv, cli = _run_pair(lambda s: tls.wrap_server(s, sctx), lambda c: tls.wrap_client(c, cctx))
    assert isinstance(srv, ssl.SSLError)  # bob does not trust mallory's CA
    assert isinstance(cli, ssl.SSLError)  # mallory does not trust bob's CA


def test_client_without_cert_rejected(certs: Path) -> None:
    sctx = tls.server_context(certs, "bob")
    cctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)  # trusts the CA but has no own cert
    cctx.check_hostname = False
    cctx.load_verify_locations(certs / "ca.crt")

    def client(sock: socket.socket) -> object:
        cs = cctx.wrap_socket(sock)
        return cs.recv(1)  # TLS 1.3: server's rejection arrives after the client's handshake

    srv, cli = _run_pair(lambda s: tls.wrap_server(s, sctx), client)
    assert isinstance(srv, ssl.SSLError)
    assert isinstance(cli, (ssl.SSLError, OSError))


def test_mode_mismatch_tls_client_vs_plain_server(certs: Path) -> None:
    cctx = tls.client_context(certs, "alice")

    def plain_server(sock: socket.socket) -> None:
        send_msg(sock, MsgType.HELLO, 1, b"{}")  # plain peer starts speaking framing

    srv, cli = _run_pair(plain_server, lambda c: tls.wrap_client(c, cctx))
    assert srv is None
    assert isinstance(cli, ssl.SSLError)


def test_failed_handshake_closes_socket(certs: Path) -> None:
    s, c = socket.socketpair()
    c.close()  # peer gone, so the handshake fails fast
    with pytest.raises((ssl.SSLError, OSError)):
        tls.wrap_server(s, tls.server_context(certs, "bob"))
    assert s.fileno() == -1  # §6.6: closed on failure


def test_handshake_timeout_applies_when_none(certs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tls, "HANDSHAKE_TIMEOUT", 0.2)  # keep the test fast
    s, c = socket.socketpair()  # c never speaks, so the server handshake must time out
    with pytest.raises((TimeoutError, ssl.SSLError, OSError)):
        tls.wrap_server(s, tls.server_context(certs, "bob"))
    c.close()


def test_original_timeout_restored_after_handshake(certs: Path) -> None:
    sctx = tls.server_context(certs, "bob")
    cctx = tls.client_context(certs, "alice")
    s, c = socket.socketpair()  # both start with no timeout
    out: dict[str, object] = {}
    t = threading.Thread(target=lambda: out.setdefault("s", tls.wrap_server(s, sctx)))
    t.start()
    cs = tls.wrap_client(c, cctx)
    t.join(TIMEOUT)
    assert cs.gettimeout() is None
    assert out["s"].gettimeout() is None  # type: ignore[attr-defined]
