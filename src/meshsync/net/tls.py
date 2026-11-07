# FROZEN INTERFACE (AGENTS.md §6): changing or adding to the public interface in this file
# needs an issue and approval from all three members first (R-6.2).
"""TLS 1.3 mutual-auth contexts and socket wrapping (PROJECT_CONTEXT.md §6.6, D9).

Owner: Muhammad Ahmed Qazi (CS-24045). Issue #5.

Usage: build a context once, then wrap each TCP socket right after accept()/connect(),
before HELLO (§6.6). Plain mode (--plain) simply never calls these functions.

Decision (e): peers dial by IP, so check_hostname=False; trust comes from CA chain
verification (CERT_REQUIRED). Certs carry SAN=DNS:<peer>. Because hostname checking is off,
the caller MUST check peer_cert_name(ssock) == the HELLO `name` (see peer_cert_name).
"""

import logging
import socket
import ssl
from pathlib import Path

from meshsync.protocol.constants import HANDSHAKE_TIMEOUT

log = logging.getLogger(__name__)


def _load(ctx: ssl.SSLContext, cert_dir: Path, name: str) -> ssl.SSLContext:
    """Shared setup: TLS 1.3 minimum, trust only the group CA, present our own cert (§6.6)."""
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.load_verify_locations(cert_dir / "ca.crt")
    ctx.load_cert_chain(cert_dir / f"{name}.crt", cert_dir / f"{name}.key")
    return ctx


def server_context(cert_dir: Path, name: str) -> ssl.SSLContext:
    """Context for accepted connections; requires a client cert from the group CA (§6.6)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.verify_mode = ssl.CERT_REQUIRED  # mutual TLS: client must present a cert
    return _load(ctx, cert_dir, name)


def client_context(cert_dir: Path, name: str) -> ssl.SSLContext:
    """Context for outgoing connections; verifies the server cert against the group CA (§6.6)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False  # decision (e): we dial by IP; must be set before verify_mode
    ctx.verify_mode = ssl.CERT_REQUIRED
    return _load(ctx, cert_dir, name)


def _wrap(sock: socket.socket, ctx: ssl.SSLContext, server_side: bool) -> ssl.SSLSocket:
    """Wrap and handshake now; errors are logged and re-raised (§10 untrusted cert/mismatch)."""
    if sock.gettimeout() is None:
        sock.settimeout(HANDSHAKE_TIMEOUT)  # never hang on a handshake (§6.7)
    try:
        return ctx.wrap_socket(sock, server_side=server_side, do_handshake_on_connect=True)
    except (ssl.SSLError, OSError) as e:
        # e.g. CERTIFICATE_VERIFY_FAILED (untrusted cert), WRONG_VERSION_NUMBER (mode mismatch)
        log.warning("TLS handshake failed (%s side): %s", "server" if server_side else "client", e)
        raise


def wrap_server(sock: socket.socket, ctx: ssl.SSLContext) -> ssl.SSLSocket:
    """TLS-wrap an accepted socket; call right after accept(), before HELLO (§6.6)."""
    return _wrap(sock, ctx, server_side=True)


def wrap_client(sock: socket.socket, ctx: ssl.SSLContext) -> ssl.SSLSocket:
    """TLS-wrap a connected socket; call right after connect(), before HELLO (§6.6)."""
    return _wrap(sock, ctx, server_side=False)


def peer_cert_name(ssock: ssl.SSLSocket) -> str:
    """Return the CN of the peer's verified certificate.

    Contract: the caller (Connection/PeerManager) MUST check this equals the `name` in the
    peer's HELLO and close the connection if not. Otherwise any valid group member could
    claim to be another member, since hostname checking is off (decision e).
    """
    cert = ssock.getpeercert()
    if not cert:
        raise ValueError("no verified peer certificate")
    for rdn in cert["subject"]:
        for key, value in rdn:
            if key == "commonName":
                return value
    raise ValueError("peer certificate has no CN")
