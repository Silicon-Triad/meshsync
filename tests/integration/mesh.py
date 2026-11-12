"""Localhost mesh harness for integration tests: each peer is a real `meshsync run` process.

Every peer gets its own sync dir, secret file, free TCP and UDP port and a log file, so a test
can check both the outcome (files on disk) and the log line that is the §10 evidence.
Peers find each other with --peer (§14 static fallback), not UDP broadcast.
"""

import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

WAIT = 30.0  # upper bound for every wait in the integration tests, seconds


def free_port(kind: int = socket.SOCK_STREAM) -> int:
    with socket.socket(socket.AF_INET, kind) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_until(cond: Callable[[], bool], timeout: float = WAIT, step: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(step)
    return cond()


def write_secret(path: Path, key: bytes) -> Path:
    path.write_text(key.hex() + "\n")
    path.chmod(0o600)
    return path


class Peer:
    """One `meshsync run` process. Logs go to <root>/<name>.log."""

    def __init__(self, root: Path, name: str, secret_file: Path, **opts: object) -> None:
        self.name = name
        self.dir = root / name
        self.dir.mkdir(parents=True, exist_ok=True)
        self.log_path = root / f"{name}.log"
        self.secret_file = secret_file
        self.tcp_port = free_port()
        self.udp_port = free_port(socket.SOCK_DGRAM)
        self.opts = opts
        self.proc: subprocess.Popen | None = None

    @property
    def addr(self) -> str:
        return f"127.0.0.1:{self.tcp_port}"

    def start(self, peers: list["Peer"] = ()) -> "Peer":
        cmd = [
            sys.executable, "-m", "meshsync", "run",
            "--dir", str(self.dir), "--name", self.name,
            "--secret-file", str(self.secret_file),
            "--tcp-port", str(self.tcp_port), "--udp-port", str(self.udp_port),
            "--config", str(self.dir.parent / "none.toml"),
            "--log-level", "INFO",
        ]  # fmt: skip
        (self.dir.parent / "none.toml").touch()  # empty config: never read ~/.meshsync
        if self.opts.get("cert_dir"):
            cmd += ["--tls", "--cert-dir", str(self.opts["cert_dir"])]
        else:
            cmd += ["--plain"]
        for p in peers:
            cmd += ["--peer", p.addr]
        log = self.log_path.open("ab")
        self.proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, env=os.environ)
        log.close()
        assert wait_until(lambda: "peer manager started" in self.log()), self.log()
        return self

    def log(self) -> str:
        return self.log_path.read_text(errors="replace") if self.log_path.exists() else ""

    def wait_log(self, text: str, timeout: float = WAIT) -> bool:
        return wait_until(lambda: text in self.log(), timeout)

    def kill(self) -> None:
        """SIGKILL: the peer vanishes without BYE or LEAVE (§10 'peer killed')."""
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(5)

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)  # main() stops the node on Ctrl-C
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.kill()
