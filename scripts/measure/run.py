#!/usr/bin/env python3
"""Experiment runner for the measurement plan (PROJECT_CONTEXT.md §11, R6). Issue #20.

Usage: scripts/measure/run.py EXPERIMENT [--runs 5] [--testdata testdata] [--data data]
       EXPERIMENT: full_delta | fixed_cdc | sources | impaired | tls | all

SINGLE HOST ONLY: every peer runs on this machine (127.0.0.1). The runner for the 3 laptops is
issue #77; until then these numbers are pilots, and all peers share this machine's CPUs and disk
(scanning, assembly and transfer compete), so laptop runs supersede them.

Experiments (§11 conditions 1-3, plain mode):
  full_delta  condition 1: sync set A's 100 MiB file (full), then its 1 KiB overwrite (delta)
  fixed_cdc   condition 2: A, then the 1 KiB insert at offset 0, with --chunking fixed vs cdc
  sources     condition 3: a fresh peer joins and fetches A from one holder vs two holders
  impaired    condition 5: A over a clean link, then with netem loss, then netem delay (#69)
  tls         condition 6: A in plain vs TLS mode, plus each receiver's setup time (#69)

Each run starts a fresh mesh (new folders, empty chunk stores) under data/raw/ (gitignored),
runs every peer through scripts/measure/peer.py so it writes its metrics CSV (#17), and writes
one summary row per receiving peer to data/<experiment>/run<k>.csv.

Columns (one host, one clock; bytes are application layer, header + body, see #56):
  e2e_from    what e2e_s starts at: "placed" (file put on the source) or, for condition 3,
              "joiner_start" (joiner process started: interpreter start, handshake and INDEX
              exchange included). Never compare e2e_s across different e2e_from values.
  e2e_s       e2e_from -> file complete on the receiver; resolution is --poll (poll_s column)
  notify_s    file placed -> source sends its first NOTIFY (the scan + chunking part of e2e_s)
  transfer_s  receiver's first CHUNK_REQ sent -> last CHUNK_DATA received
  chunk_bytes CHUNK_DATA bytes received; sources/source_bytes say which peers sent them (with
              3 peers, receivers also fetch from each other, so this is not source -> receiver only;
              use --peers 2 for a point-to-point number)
  suspect     "dup" if chunk_bytes > 1.1 x file size: chunks were downloaded more than once
              (#60). Such rows must not be used; condition 3 retries the run instead.
  setup_s     condition 6 only (empty elsewhere): a receiver's "peer manager started" log line ->
              its "connection to <source> is SYNCED" line, from its own log (ms resolution):
              TCP connect, TLS handshake (tls mode), HMAC handshake and INDEX exchange.
Condition 5 impairs --iface (default lo) with scripts/netem.sh, which needs root: run as root or
with passwordless sudo. On lo every packet crosses the qdisc in both directions, unlike netem on
one laptop's Wi-Fi link (outgoing packets only):
  delay  a D ms delay adds 2D ms to each round trip.
  loss   data and the returning ACKs are each dropped with probability p, so a request/reply
         exchange survives with (1 - p)^2: about 9.75 % fails at p = 5 %, not 5 %. The loss rows
         are therefore harsher than a single 5 % link; say so wherever they are quoted.
Test data comes from scripts/gen_testdata.py (data set decided in #25): sets A and D.
Owner: Huzaifa Hanif (CS-24039).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from meshsync.net.auth import SECRET_BYTES
from meshsync.protocol.constants import META_DIR, TCP_PORT, UDP_PORT

log = logging.getLogger("measure")

REPO = Path(__file__).resolve().parents[2]
PEER_PY = Path(__file__).resolve().with_name("peer.py")
RUNS_DEFAULT = 5  # §11: 5 runs per condition
TARGET = "large.bin"  # name of the measured file inside every sync folder
FIELDS = (
    "experiment",
    "variant",
    "run",
    "receiver",
    "file",
    "size",
    "e2e_s",
    "notify_s",
    "transfer_s",
    "chunk_msgs",
    "chunk_bytes",
    "recv_bytes",
    "sent_bytes",
    "sources",
    "source_bytes",
    "e2e_from",
    "poll_s",
    "suspect",
    "setup_s",
)
NETEM_SH = REPO / "scripts" / "netem.sh"
GEN_CERTS_SH = REPO / "scripts" / "gen_certs.sh"
LOSS_PCT_DEFAULT = 5.0  # §10: "5 % loss via netem"
DELAY_MS_DEFAULT = 50.0  # condition 5 delay: team decision recorded on #69
DUP_FACTOR = 1.1  # harness check: more chunk bytes than this x file size = duplicate downloads
_SYNCED_RE = re.compile(r"connection to (\S+) is SYNCED")
_LOG_TS = r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3}) "  # logging's default asctime
_STARTED_RE = re.compile(_LOG_TS + r".*peer manager started", re.M)


# ---------------------------------------------------------------- metrics CSV (pure, tested)


@dataclass(frozen=True)
class Msg:
    t: float
    direction: str
    peer: str
    type: str
    size: int


def read_metrics(path: Path) -> list[Msg]:
    """Rows of a metrics CSV written by peer.py (metrics.CSV_HEADER: t,direction,peer,type,size)."""
    with path.open(newline="", encoding="utf-8") as f:
        return [
            Msg(float(r["t"]), r["direction"], r["peer"], r["type"], int(r["size"]))
            for r in csv.DictReader(f)
        ]


@dataclass
class Window:
    """What one peer sent and received between t0 and t1 (inclusive)."""

    transfer_s: float | None = None
    chunk_msgs: int = 0
    chunk_bytes: int = 0
    recv_bytes: int = 0
    sent_bytes: int = 0
    source_bytes: dict[str, int] = field(default_factory=dict)
    first_notify: float | None = None


def window(msgs: Iterable[Msg], t0: float, t1: float) -> Window:
    w = Window()
    first_req: float | None = None
    last_data: float | None = None
    for m in msgs:
        if not t0 <= m.t <= t1:
            continue
        if m.direction == "recv":
            w.recv_bytes += m.size
            if m.type == "CHUNK_DATA":
                w.chunk_msgs += 1
                w.chunk_bytes += m.size
                w.source_bytes[m.peer] = w.source_bytes.get(m.peer, 0) + m.size
                last_data = m.t if last_data is None else max(last_data, m.t)
        else:
            w.sent_bytes += m.size
            if m.type == "CHUNK_REQ":
                first_req = m.t if first_req is None else min(first_req, m.t)
            elif m.type == "NOTIFY":
                w.first_notify = m.t if w.first_notify is None else min(w.first_notify, m.t)
    if first_req is not None and last_data is not None:
        # A send is recorded after sendall() returns, so for a one-chunk fetch the reply's
        # recv row can carry an earlier time (by microseconds); that is a zero-length transfer.
        w.transfer_s = max(0.0, last_data - first_req)
    return w


def read_manifest(testdata: Path) -> dict[str, str]:
    """MANIFEST.sha256 from gen_testdata.py: relative path -> sha256 hex."""
    out = {}
    for line in (testdata / "MANIFEST.sha256").read_text(encoding="utf-8").splitlines():
        digest, _, rel = line.partition("  ")
        if rel:
            out[rel] = digest
    return out


def place(src: Path, sync_dir: Path, rel: str) -> float:
    """Copy src into sync_dir/rel so it appears atomically; return the time it appeared.

    The copy is staged under .meshsync/ (never scanned, §7.3) on the same filesystem, then
    os.replace'd, so the scanner never sees a half-copied file and the copy time is not measured.
    """
    stage = sync_dir / META_DIR / f"measure-stage-{secrets.token_hex(4)}"
    stage.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copyfile(src, stage)
        dest = sync_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(stage, dest)
    except BaseException:
        stage.unlink(missing_ok=True)
        raise
    return time.time()


def fmt(x: float | None) -> str:
    return "" if x is None else f"{x:.6f}"


# ---------------------------------------------------------------- local mesh


@dataclass
class Peer:
    name: str
    root: Path
    port: int
    proc: subprocess.Popen | None = None

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def csv(self) -> Path:
        return self.root / "metrics.csv"

    @property
    def log(self) -> Path:
        return self.root / "peer.log"


class LocalMesh:
    """N peers on this machine, every one listed as a static peer of the others (§14)."""

    def __init__(
        self,
        base: Path,
        n: int,
        chunking: str,
        tcp_base: int,
        udp_port: int,
        keep: bool = False,
        tls: bool = False,
    ) -> None:
        self.base = base
        self.tls = tls
        self.keep = keep  # keep the sync folders (and their 100 MiB copies) after the run
        self.chunking = chunking
        self.udp_port = udp_port
        base.mkdir(parents=True, exist_ok=True)
        self.key = base / "key"
        self.key.write_text(secrets.token_hex(SECRET_BYTES) + "\n")
        self.key.chmod(0o600)
        self.config = base / "empty.toml"  # keeps ~/.meshsync/config.toml out of the run
        self.config.write_text("")
        self.peers = [Peer(f"p{k}", base / f"p{k}", tcp_base + k) for k in range(1, n + 1)]
        for p in self.peers:
            p.data.mkdir(parents=True, exist_ok=True)
        self.certs = base / "certs"  # under data/raw/ (gitignored); fresh CA per mesh
        if tls:
            names = [p.name for p in self.peers]
            subprocess.run(
                [str(GEN_CERTS_SH), str(self.certs), *names],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )  # fmt: skip

    def peer_args(self, p: Peer) -> list[str]:
        """The peer.py command line for p: every other peer is a static peer (§14)."""
        statics = [a for q in self.peers if q is not p for a in ("--peer", f"127.0.0.1:{q.port}")]
        mode = ["--tls", "--cert-dir", str(self.certs)] if self.tls else ["--plain"]
        return [
            sys.executable, str(PEER_PY), str(p.csv), "--",
            "--dir", str(p.data), "--name", p.name, "--secret-file", str(self.key),
            "--config", str(self.config), *mode, "--chunking", self.chunking,
            "--tcp-port", str(p.port), "--udp-port", str(self.udp_port), *statics,
        ]  # fmt: skip

    def start(self, peers: list[Peer] | None = None) -> None:
        for p in peers or self.peers:
            with p.log.open("w") as out:
                p.proc = subprocess.Popen(self.peer_args(p), stdout=out, stderr=subprocess.STDOUT)

    def stop(self) -> None:
        running = [p for p in self.peers if p.proc and p.proc.poll() is None]
        for p in running:
            p.proc.send_signal(signal.SIGINT)  # clean node.stop() (peer.py runs meshsync run)
        for p in running:
            try:
                p.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                log.warning("%s did not stop on SIGINT; killing it", p.name)
                p.proc.kill()
                p.proc.wait()

    def check_alive(self) -> None:
        for p in self.peers:
            if p.proc and p.proc.poll() is not None:
                raise RuntimeError(f"{p.name} exited with {p.proc.returncode}; see {p.log}")

    def __enter__(self) -> LocalMesh:
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
        if not self.keep:  # metrics.csv and peer.log stay; the file copies and stores go
            for p in self.peers:
                shutil.rmtree(p.data, ignore_errors=True)


def wait_until(
    cond: Callable[[], bool], timeout: float, poll: float, what: str, mesh: LocalMesh
) -> None:
    end = time.monotonic() + timeout
    while not cond():
        mesh.check_alive()
        if time.monotonic() > end:
            raise TimeoutError(f"timed out after {timeout:.0f} s waiting for {what}")
        time.sleep(poll)


def connected(p: Peer, others: list[Peer]) -> bool:
    """p's log says its connection to every peer in others reached SYNCED (§6.7)."""
    try:
        names = set(_SYNCED_RE.findall(p.log.read_text(errors="replace")))
    except FileNotFoundError:
        return False
    return {q.name for q in others} <= names


def _log_time(stamp: str) -> float:
    return datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S,%f").timestamp()


def setup_time(log_text: str, other: str) -> float | None:
    """Seconds from "peer manager started" to "connection to <other> is SYNCED" (condition 6).

    Both lines come from the same process clock, so the difference needs no clock sync.
    None when either line is missing.
    """
    started = _STARTED_RE.search(log_text)
    synced = re.search(_LOG_TS + rf".*connection to {re.escape(other)} is SYNCED", log_text, re.M)
    if not started or not synced:
        return None
    return _log_time(synced.group(1)) - _log_time(started.group(1))


def started(p: Peer) -> bool:
    try:
        return _STARTED_RE.search(p.log.read_text(errors="replace")) is not None
    except FileNotFoundError:
        return False


def indexed(p: Peer, rel: str, sha: str) -> bool:
    """p's local index (sync/index.py, <root>/.meshsync/index.json) holds rel with this hash."""
    try:
        data = json.loads((p.data / META_DIR / "index.json").read_text())
    except (FileNotFoundError, ValueError):
        return False
    return any(e["path"] == rel and e["file_hash"] == sha for e in data.get("entries", []))


def ino(path: Path) -> int | None:
    try:
        return path.stat().st_ino
    except FileNotFoundError:
        return None


def wait_files(
    receivers: list[Peer], size: int, before: dict[str, int | None], args: argparse.Namespace,
    mesh: LocalMesh,
) -> dict[str, float]:  # fmt: skip
    """Time each receiver's TARGET is replaced by a file of `size` (the assembler's os.replace)."""
    done: dict[str, float] = {}
    end = time.monotonic() + args.timeout
    while len(done) < len(receivers):
        for p in receivers:
            if p.name in done:
                continue
            path = p.data / TARGET
            try:
                st = path.stat()
            except FileNotFoundError:
                continue
            if st.st_ino != before.get(p.name) and st.st_size == size:
                done[p.name] = time.time()
        mesh.check_alive()
        if time.monotonic() > end:
            missing = [p.name for p in receivers if p.name not in done]
            raise TimeoutError(f"{missing} did not get {TARGET} within {args.timeout:.0f} s")
        time.sleep(args.poll)
    return done


def verify(receivers: list[Peer], sha: str) -> None:
    """Hash every receiver's copy after timing, so hashing never competes with a transfer."""
    for p in receivers:
        h = sha256_file(p.data / TARGET)
        if h != sha:
            raise RuntimeError(f"{p.name}: {TARGET} has sha256 {h}, expected {sha}")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(1 << 20):
            h.update(block)
    return h.hexdigest()


# ---------------------------------------------------------------- one measured step


@dataclass
class Step:
    experiment: str
    variant: str
    run: int
    file: str  # test data path relative to --testdata


def suspect(chunk_bytes: int, size: int) -> str:
    return "dup" if chunk_bytes > DUP_FACTOR * size else ""


def measure(
    step: Step, mesh: LocalMesh, source: Peer, receivers: list[Peer], t0: float,
    done: dict[str, float], size: int, poll: float, e2e_from: str = "placed",
) -> list[dict[str, str]]:  # fmt: skip
    # No wait needed: peer.py's FileHandler flushes every row, and a receiver's CHUNK_DATA rows
    # are written before its file is assembled, so they are on disk once `done` was seen.
    src = window(read_metrics(source.csv), t0, max(done.values()))
    rows = []
    for p in receivers:
        w = window(read_metrics(p.csv), t0, done[p.name])
        rows.append(
            {
                "experiment": step.experiment,
                "variant": step.variant,
                "run": str(step.run),
                "receiver": p.name,
                "file": step.file,
                "size": str(size),
                "e2e_s": fmt(done[p.name] - t0),
                "notify_s": fmt(None if src.first_notify is None else src.first_notify - t0),
                "transfer_s": fmt(w.transfer_s),
                "chunk_msgs": str(w.chunk_msgs),
                "chunk_bytes": str(w.chunk_bytes),
                "recv_bytes": str(w.recv_bytes),
                "sent_bytes": str(w.sent_bytes),
                "sources": str(len(w.source_bytes)),
                "source_bytes": ";".join(f"{k}:{v}" for k, v in sorted(w.source_bytes.items())),
                "e2e_from": e2e_from,
                "poll_s": fmt(poll),
                "suspect": suspect(w.chunk_bytes, size),
            }
        )
    return rows


def push(
    step: Step, mesh: LocalMesh, source: Peer, receivers: list[Peer], args: argparse.Namespace,
    manifest: dict[str, str],
) -> list[dict[str, str]]:  # fmt: skip
    """Put step.file on source as TARGET and measure until every receiver has it."""
    src = args.testdata / step.file
    size = src.stat().st_size
    before = {p.name: ino(p.data / TARGET) for p in receivers}
    t0 = place(src, source.data, TARGET)
    done = wait_files(receivers, size, before, args, mesh)
    verify(receivers, manifest[step.file])
    rows = measure(step, mesh, source, receivers, t0, done, size, args.poll)
    for r in rows:
        if r["suspect"]:
            log.warning("%s/%s run %s %s: %s chunk bytes for a %s-byte file (duplicates, #60)",
                        r["experiment"], r["variant"], r["run"], r["receiver"], r["chunk_bytes"],
                        r["size"])  # fmt: skip
        log.info(
            "%s/%s run %s %s: e2e %ss transfer %ss %s chunk bytes from %s",
            r["experiment"], r["variant"], r["run"], r["receiver"], r["e2e_s"][:6],
            r["transfer_s"][:6], r["chunk_bytes"], r["source_bytes"],
        )  # fmt: skip
    return rows


def new_mesh(
    args: argparse.Namespace, exp: str, run: int, tag: str, n: int, chunking: str,
    tls: bool = False,
) -> LocalMesh:  # fmt: skip
    base = args.data / "raw" / exp / f"run{run}" / tag
    if base.exists():
        shutil.rmtree(base)
    return LocalMesh(base, n, chunking, args.tcp_base, args.udp_port, args.keep_data, tls)


def start_connected(mesh: LocalMesh, peers: list[Peer], args: argparse.Namespace) -> None:
    mesh.start(peers)
    for p in peers:
        others = [q for q in peers if q is not p]
        wait_until(lambda p=p, o=others: connected(p, o), args.timeout, args.poll, "links", mesh)


# ---------------------------------------------------------------- experiments (§11)

A = "large/large.bin"  # set A (#25)
OVERWRITE = "edits/overwrite.bin"  # set D
INSERT = "edits/insert.bin"  # set D


def exp_full_delta(run: int, args: argparse.Namespace, manifest: dict[str, str]) -> list[dict]:
    """§11 condition 1: full sync of A, then delta sync after a 1 KiB overwrite mid-file."""
    with new_mesh(args, "full_delta", run, "cdc", args.peers, "cdc") as mesh:
        src, *rcv = mesh.peers
        start_connected(mesh, mesh.peers, args)
        rows = push(Step("full_delta", "full", run, A), mesh, src, rcv, args, manifest)
        rows += push(Step("full_delta", "delta", run, OVERWRITE), mesh, src, rcv, args, manifest)
    return rows


def exp_fixed_cdc(run: int, args: argparse.Namespace, manifest: dict[str, str]) -> list[dict]:
    """§11 condition 2 (backs D10): 1 KiB inserted at offset 0, fixed vs content-defined."""
    rows: list[dict] = []
    for mode in ("fixed", "cdc"):
        with new_mesh(args, "fixed_cdc", run, mode, args.peers, mode) as mesh:
            src, *rcv = mesh.peers
            start_connected(mesh, mesh.peers, args)
            push(Step("fixed_cdc", f"{mode}-base", run, A), mesh, src, rcv, args, manifest)
            rows += push(Step("fixed_cdc", mode, run, INSERT), mesh, src, rcv, args, manifest)
    return rows


def exp_sources(run: int, args: argparse.Namespace, manifest: dict[str, str]) -> list[dict]:
    """§11 condition 3: a fresh peer fetches A from one holder vs from two holders.

    The holders get A before they start, so it is in their index and chunk store. The joiner
    starts last; e2e_s here runs from the joiner's start (e2e_from = "joiner_start": interpreter
    start, handshake and INDEX exchange included), and transfer_s is the chunk transfer alone.
    An attempt is retried (see --retries) when the joiner fetched from fewer peers than there are
    holders (the second link came up after the fetch began), or when it downloaded chunks more
    than once (suspect = "dup", the #60 bug): only clean runs are written.
    """
    rows: list[dict] = []
    for holders in (1, 2):
        variant = f"{holders}src"
        for attempt in range(1, args.retries + 2):
            with new_mesh(args, "sources", run, variant, holders + 1, "cdc") as mesh:
                *hs, joiner = mesh.peers
                for h in hs:
                    place(args.testdata / A, h.data, TARGET)
                start_connected(mesh, hs, args)
                for h in hs:
                    wait_until(lambda h=h: indexed(h, TARGET, manifest[A]), args.timeout,
                               args.poll, f"{h.name} to index {TARGET}", mesh)  # fmt: skip
                size = (args.testdata / A).stat().st_size
                t0 = time.time()
                mesh.start([joiner])
                done = wait_files([joiner], size, {joiner.name: None}, args, mesh)
                verify([joiner], manifest[A])
                got = measure(Step("sources", variant, run, A), mesh, hs[0], [joiner], t0,
                              done, size, args.poll, "joiner_start")  # fmt: skip
            if int(got[0]["sources"]) == holders and not got[0]["suspect"]:
                rows += got
                break
            log.warning("%s run %d attempt %d: %s source(s), %s chunk bytes%s; retrying",
                        variant, run, attempt, got[0]["sources"], got[0]["chunk_bytes"],
                        " (duplicates, #60)" if got[0]["suspect"] else "")  # fmt: skip
        else:
            raise RuntimeError(f"{variant} run {run}: no clean {holders}-source attempt")
    return rows


# ---------------------------------------------------------------- condition 5: netem (#69)


def netem_cmd(*args: str) -> list[str]:
    """scripts/netem.sh with args; through `sudo -n` when not root (fails fast, never prompts)."""
    cmd = [str(NETEM_SH), *args]
    return cmd if os.geteuid() == 0 else ["sudo", "-n", *cmd]


def impaired_profiles(args: argparse.Namespace) -> list[tuple[str, list[str] | None]]:
    """(variant, netem.sh apply arguments) for condition 5; None = clean link."""
    return [
        ("clean", None),
        ("loss", ["loss", f"{args.loss_pct:g}"]),
        ("delay", ["delay", f"{args.delay_ms:g}"]),
    ]


@contextmanager
def netem(iface: str, profile: list[str] | None) -> Iterator[None]:
    """Impair iface for the duration of the block; always cleared afterwards, even on errors."""
    subprocess.run(netem_cmd("clear", iface), check=True)
    try:
        if profile is not None:
            subprocess.run(netem_cmd("apply", iface, *profile), check=True)
            log.info("netem on %s: %s", iface, " ".join(profile))
        yield
    finally:
        subprocess.run(netem_cmd("clear", iface), check=True)


def exp_impaired(run: int, args: argparse.Namespace, manifest: dict[str, str]) -> list[dict]:
    """§11 condition 5: A over a clean link, then with netem loss, then netem delay.

    The mesh connects on a clean link; netem is applied just before A is placed and cleared
    as soon as every receiver has it, so only the data transfer is impaired.
    """
    rows: list[dict] = []
    for variant, profile in impaired_profiles(args):
        with new_mesh(args, "impaired", run, variant, args.peers, "cdc") as mesh:
            src, *rcv = mesh.peers
            start_connected(mesh, mesh.peers, args)
            with netem(args.iface, profile):
                rows += push(Step("impaired", variant, run, A), mesh, src, rcv, args, manifest)
    return rows


# ---------------------------------------------------------------- condition 6: plain vs TLS (#69)


def exp_tls(run: int, args: argparse.Namespace, manifest: dict[str, str]) -> list[dict]:
    """§11 condition 6: setup time and transfer of A in plain vs TLS mode (§12 security tension).

    The source starts first; each receiver then starts alone and dials the peers already up,
    so its setup_s (started -> SYNCED with the source) is not inflated by a reconnect wait.
    """
    rows: list[dict] = []
    for mode in ("plain", "tls"):
        with new_mesh(args, "tls", run, mode, args.peers, "cdc", tls=mode == "tls") as mesh:
            src, *rcv = mesh.peers
            mesh.start([src])
            wait_until(lambda s=src: started(s), args.timeout, args.poll, f"{src.name} start", mesh)
            up = [src]
            for r in rcv:
                mesh.start([r])
                wait_until(lambda r=r, u=list(up): connected(r, u), args.timeout, args.poll,
                           f"{r.name} links", mesh)  # fmt: skip
                up.append(r)
            for p in up:  # every link SYNCED before timing the transfer
                others = [q for q in up if q is not p]
                wait_until(lambda p=p, o=others: connected(p, o), args.timeout, args.poll,
                           "links", mesh)  # fmt: skip
            got = push(Step("tls", mode, run, A), mesh, src, rcv, args, manifest)
            for row, r in zip(got, rcv, strict=True):
                row["setup_s"] = fmt(setup_time(r.log.read_text(errors="replace"), src.name))
            rows += got
    return rows


EXPERIMENTS: dict[str, Callable[[int, argparse.Namespace, dict[str, str]], list[dict]]] = {
    "full_delta": exp_full_delta,
    "fixed_cdc": exp_fixed_cdc,
    "sources": exp_sources,
    "impaired": exp_impaired,
    "tls": exp_tls,
}


def clear_runs(folder: Path) -> None:
    """Remove old run<k>.csv files, so a batch with fewer --runs leaves no stale results."""
    for old in sorted(folder.glob("run*.csv")):
        log.info("removing old %s", old)
        old.unlink()


def write_rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("experiment", choices=[*EXPERIMENTS, "all"])
    p.add_argument("--runs", type=int, default=RUNS_DEFAULT, help="runs per condition (§11: 5)")
    p.add_argument("--testdata", type=Path, default=REPO / "testdata",
                   help="output of scripts/gen_testdata.py with sets A and D")  # fmt: skip
    p.add_argument("--data", type=Path, default=REPO / "data", help="results folder")
    p.add_argument("--peers", type=int, default=3, help="peers for conditions 1-2 (R3: >= 3)")
    p.add_argument("--tcp-base", type=int, default=TCP_PORT - 1,
                   help="peer k listens on TCP_BASE+k (default: p1 on TCP_PORT)")  # fmt: skip
    p.add_argument("--udp-port", type=int, default=UDP_PORT)
    # Harness settings, not protocol values: how long and how often the runner itself waits.
    p.add_argument("--timeout", type=float, default=600.0, help="seconds per wait (harness)")
    p.add_argument("--poll", type=float, default=0.05, help="seconds between checks (harness)")
    p.add_argument("--keep-data", action="store_true", help="keep sync folders under data/raw/")
    p.add_argument("--retries", type=int, default=3, help="extra attempts for a 2src run")
    p.add_argument("--iface", default="lo", help="interface netem impairs (condition 5)")
    p.add_argument("--loss-pct", type=float, default=LOSS_PCT_DEFAULT,
                   help="condition 5 loss %% (default 5, §10)")  # fmt: skip
    p.add_argument("--delay-ms", type=float, default=DELAY_MS_DEFAULT,
                   help="condition 5 delay ms (default 50, #69)")  # fmt: skip
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    try:
        manifest = read_manifest(args.testdata)
    except FileNotFoundError:
        p.error(f"no {args.testdata}/MANIFEST.sha256; run scripts/gen_testdata.py first")
    missing = [f for f in (A, OVERWRITE, INSERT) if f not in manifest]
    if missing:
        p.error(
            f"test data lacks {missing}; run: scripts/gen_testdata.py {args.testdata} --sets A,D"
        )

    names = list(EXPERIMENTS) if args.experiment == "all" else [args.experiment]
    failed: list[str] = []
    for name in names:
        clear_runs(args.data / name)
        for run in range(1, args.runs + 1):
            try:
                rows = EXPERIMENTS[name](run, args, manifest)
            except Exception:  # noqa: BLE001 - one bad run must not lose the rest of the batch
                log.exception("%s run %d failed; continuing", name, run)
                failed.append(f"{name} run {run}")
                continue
            out = args.data / name / f"run{run}.csv"
            write_rows(out, rows)
            log.info("wrote %s", out)
    if failed:
        log.error("%d run(s) failed: %s", len(failed), ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
