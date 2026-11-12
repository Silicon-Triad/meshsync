"""Helpers for multi-peer integration tests: real `meshsync run` processes on 127.0.0.1 (#62).

Each peer gets its own --dir and --tcp-port, like scripts/run_local_mesh.sh (§14). Peers find
each other through static --peer addresses unless a test asks for UDP discovery. Waits poll
with a deadline derived from protocol/constants.py, never a fixed sleep (R-4.4, R-10.7).
"""

import hashlib
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from meshsync.net.auth import SECRET_BYTES
from meshsync.protocol.constants import (
    ANNOUNCE_INTERVAL,
    HANDSHAKE_TIMEOUT,
    PEER_EXPIRY,
    PING_INTERVAL,
    SCAN_INTERVAL,
)

# How long a change may take to reach every peer: connect + handshake (HANDSHAKE_TIMEOUT),
# discovery (PEER_EXPIRY covers several ANNOUNCE_INTERVALs), a static-peer redial (up to
# PING_INTERVAL) and a few scans (SCAN_INTERVAL) on both sides. Generous so slow CI passes;
# a passing test returns as soon as the condition holds.
SYNC_TIMEOUT = HANDSHAKE_TIMEOUT + PEER_EXPIRY + PING_INTERVAL + 4 * SCAN_INTERVAL
# Long enough for a scanner to have indexed a file written before or while the node runs.
SETTLE = 2 * SCAN_INTERVAL
POLL = 0.1

assert ANNOUNCE_INTERVAL < PEER_EXPIRY  # discovery can find a peer within SYNC_TIMEOUT

# Human metrics line from metrics/logger.py at DEBUG (§11 "bytes on wire").
_METRIC_RE = re.compile(r" (send|recv) peer=(\S+) type=(\w+) size=(\d+) t=")


def free_port(kind: int = socket.SOCK_STREAM) -> int:
    with socket.socket(socket.AF_INET, kind) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def wait_until(cond: Callable[[], bool], what: str, timeout: float = SYNC_TIMEOUT) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        time.sleep(POLL)
    raise AssertionError(f"timed out after {timeout:.0f}s waiting for {what}")


def settle() -> None:
    """Let the scanner see a local write (it polls every SCAN_INTERVAL, §8.3)."""
    time.sleep(SETTLE)


@dataclass
class Peer:
    name: str
    dir: Path
    log: Path
    tcp_port: int
    args: list[str] = field(default_factory=list)
    proc: subprocess.Popen | None = None

    def start(self) -> None:
        assert self.proc is None, f"{self.name} already running"
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        with self.log.open("a") as out:
            self.proc = subprocess.Popen(
                [sys.executable, "-m", "meshsync", "run", *self.args],
                stdout=out,
                stderr=subprocess.STDOUT,
                env=env,
            )

    def stop(self) -> None:
        if self.proc is None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=HANDSHAKE_TIMEOUT)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self.proc = None

    def kill(self) -> None:
        """SIGKILL: the peer vanishes without BYE or LEAVE (§10 "peer killed", #63)."""
        if self.proc is None:
            return
        self.proc.kill()
        self.proc.wait()
        self.proc = None

    def wait_log(self, text: str, what: str = "", timeout: float = SYNC_TIMEOUT) -> None:
        """Wait until `text` is in this peer's log; on timeout the message holds the log tail."""
        try:
            wait_until(lambda: text in self.log_text(), what or repr(text), timeout)
        except AssertionError as e:
            raise AssertionError(f"{e}\n===== {self.name}\n{self.log_text()[-4000:]}") from None

    def peer_id(self) -> str:
        """Own peer_id, persisted by node.py in <dir>/.meshsync/peer_id (decision (l))."""
        return (self.dir / ".meshsync" / "peer_id").read_text().strip()

    def path(self, rel: str) -> Path:
        return self.dir / rel

    def log_text(self) -> str:
        return self.log.read_text(errors="replace") if self.log.exists() else ""

    def recv_bytes(self, msg_type: str, since: int = 0) -> int:
        """Bytes on the wire received in msg_type messages, from log offset `since` on."""
        total = 0
        for m in _METRIC_RE.finditer(self.log_text()[since:]):
            if m.group(1) == "recv" and m.group(3) == msg_type:
                total += int(m.group(4))
        return total

    def files(self) -> set[str]:
        return {
            p.relative_to(self.dir).as_posix()
            for p in self.dir.rglob("*")
            if p.is_file() and ".meshsync" not in p.relative_to(self.dir).parts
        }


class Mesh:
    """N peers on 127.0.0.1. Call start() after writing any files the peers should begin with."""

    def __init__(
        self,
        root: Path,
        names: list[str],
        *,
        tls: bool = False,
        discovery: bool = False,
        isolated: bool = False,
        own_secret: frozenset[str] = frozenset(),
        cert_dir: dict[str, Path] | None = None,
        plain: frozenset[str] = frozenset(),
    ) -> None:
        """own_secret: these peers get a different random secret (§10 "wrong secret").
        cert_dir: per-peer --cert-dir override, e.g. a rogue CA (§10 "untrusted cert").
        plain: these peers run --plain even when tls=True (§10 "mode mismatch")."""
        self.root = root
        root.mkdir(parents=True)
        self.secret = root / "key"
        self.secret.write_text(secrets.token_hex(SECRET_BYTES) + "\n")
        self.secret.chmod(0o600)
        self.udp_port = free_port(socket.SOCK_DGRAM)
        self.certs = root / "certs"
        if tls:
            if shutil.which("openssl") is None:
                pytest.skip("openssl not on PATH; needed by scripts/gen_certs.sh")
            repo = Path(__file__).resolve().parents[2]
            subprocess.run(
                [str(repo / "scripts" / "gen_certs.sh"), str(self.certs), *names],
                check=True,
                capture_output=True,
            )
        self.peers: dict[str, Peer] = {}
        for name in names:
            d = root / name
            (d / "data").mkdir(parents=True)
            self.peers[name] = Peer(name, d / "data", d / "peer.log", free_port())
        for p in self.peers.values():
            certs = (cert_dir or {}).get(p.name, self.certs)
            use_tls = tls and p.name not in plain
            mode = ["--tls", "--cert-dir", str(certs)] if use_tls else ["--plain"]
            secret = self.secret
            if p.name in own_secret:
                secret = root / f"key-{p.name}"
                secret.write_text(secrets.token_hex(SECRET_BYTES) + "\n")
                secret.chmod(0o600)
            p.args = [
                "--dir", str(p.dir), "--name", p.name, "--secret-file", str(secret),
                "--tcp-port", str(p.tcp_port), *mode, "--log-level", "DEBUG",
            ]  # fmt: skip
            if isolated:  # own discovery port, no static peers: sees nobody
                p.args += ["--udp-port", str(free_port(socket.SOCK_DGRAM))]
                continue
            p.args += ["--udp-port", str(self.udp_port)]
            if not discovery:
                for q in self.peers.values():
                    if q is not p:
                        p.args += ["--peer", f"127.0.0.1:{q.tcp_port}"]

    def __getitem__(self, name: str) -> Peer:
        return self.peers[name]

    def start(self) -> None:
        for p in self.peers.values():
            p.start()

    def stop(self) -> None:
        for p in self.peers.values():
            p.stop()

    def wait(self, cond: Callable[[], bool], what: str) -> None:
        """wait_until, with the tail of every peer log in the failure message."""
        try:
            wait_until(cond, what)
        except AssertionError as e:
            raise AssertionError(f"{e}\n{self.logs()}") from None

    def logs(self) -> str:
        return "\n".join(f"===== {p.name}\n{p.log_text()[-4000:]}" for p in self.peers.values())
