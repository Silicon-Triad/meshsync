#!/usr/bin/env python3
"""Run the §11 experiments across separate hosts (PROJECT_CONTEXT.md §11, R6, R3). Issue #77.

Usage: scripts/measure/hosts.py EXPERIMENT HOST HOST [HOST ...] [--runs 5] [--tls] ...
       EXPERIMENT: full_delta | fixed_cdc | sources | impaired | tls | all
       HOST: NAME:IP[:SSH_TARGET[:TCP_PORT]]   (no SSH_TARGET = this machine)
  e.g. scripts/measure/hosts.py full_delta kratos:192.168.100.92 \
         minipc:192.168.100.190:user@192.168.100.190 defalt:192.168.100.80:user@192.168.100.80

Run it on one host (the driver); the others are reached over SSH, one reused connection each
(one password prompt per host). The FIRST host is the source; the others receive. For
`sources` the last host is the joiner. For `impaired` the source must be this machine: netem is
applied to its --iface (default wlp2s0) through sudo: asked once at the start (`sudo -v`), kept
alive every minute for the whole run, and clearing netem falls back to an interactive sudo
rather than ever leaving the Wi-Fi impaired (#91).

Every host needs the repo at ~/meshsync (--remote-repo) with its .venv, the shared secret at
~/.meshsync/key, certs named after the host in ~/.meshsync/certs (for --tls / `tls`), and the
#25 test data (`scripts/gen_testdata.py testdata --sets A,D`: seeded, so identical on every
host; the driver checks the SHA-256 before a run). Mini PC Wi-Fi power saving off (#83).

One clock per number (the #77 problem): hosts' clocks are never compared.
  e2e_s       receiver's first NOTIFY received -> its last CHUNK_DATA received (e2e_from
              "first_notify"); for `sources`, joiner's process start -> last CHUNK_DATA
              ("joiner_start"). Receiver clock only.
  notify_s    file placed on the source -> source's first NOTIFY sent. Source clock only.
  transfer_s, bytes, sources, suspect, setup_s: as scripts/measure/run.py (one host each).
Output: data/<experiment>/run<k>.csv in run.py's columns (analysis/analyze.py reads it), raw
per-host metrics CSVs and logs under data/raw/hosts/<experiment>/run<k>/<tag>/<host>/.
Owner: Huzaifa Hanif (CS-24039); written for #77 by Mujtaba Jawaid Rao.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run as mr  # noqa: E402 - sibling script: metrics parsing, row format, netem, data set

from meshsync.protocol.constants import META_DIR, TCP_PORT, UDP_PORT  # noqa: E402

log = logging.getLogger("measure.hosts")

SSH_OPTS = ["-o", "ControlMaster=auto", "-o", "ControlPath=~/.ssh/cm-hosts-%r@%h:%p",
            "-o", "ControlPersist=30m",
            # Detect a dead link in about a minute instead of hanging (real batch, #77).
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4"]  # fmt: skip
SSH_FAIL = 255  # ssh's own exit status: connection failed, not the remote command
SSH_RETRIES = 3  # harness: extra attempts for a read-only remote command after an SSH failure
SSH_RETRY_WAIT_S = 3  # harness: seconds between those attempts
STOP_WAIT_S = 10  # harness: seconds a peer gets to exit after SIGINT before SIGKILL
SUDO_KEEPALIVE_S = 60  # harness: refresh cached sudo well inside sudo's default 5 min timeout
_SYNCED_RE = re.compile(r"connection to (\S+) is SYNCED")


# ---------------------------------------------------------------- hosts and remote commands


@dataclass
class Host:
    name: str
    ip: str
    target: str | None = None  # SSH target; None = this machine
    port: int = TCP_PORT
    home: str = ""  # filled in by resolve()
    repo: str = ""

    @property
    def local(self) -> bool:
        return self.target is None


def parse_host(spec: str) -> Host:
    """NAME:IP[:SSH_TARGET[:TCP_PORT]]; an empty SSH_TARGET means this machine."""
    parts = spec.split(":")
    if len(parts) < 2 or len(parts) > 4 or not parts[0] or not parts[1]:
        raise ValueError(f"bad host {spec!r}: want NAME:IP[:SSH_TARGET[:TCP_PORT]]")
    target = parts[2] if len(parts) > 2 and parts[2] else None
    port = int(parts[3]) if len(parts) > 3 and parts[3] else TCP_PORT
    return Host(parts[0], parts[1], target, port)


def shell_argv(host: Host, script: str) -> list[str]:
    """argv that runs a bash script on host: locally, or over the reused SSH connection."""
    if host.local:
        return ["bash", "-c", script]
    return ["ssh", *SSH_OPTS, host.target, "bash -c " + shlex.quote(script)]


def sh(host: Host, script: str, check: bool = True, retry: bool = False) -> str:
    """Run script on host. retry=True is only for read-only scripts (polls, hashes, clock
    reads): they are re-sent when SSH itself fails (exit 255, e.g. the shared connection
    dropped on a Wi-Fi blip), which cannot repeat a side effect. Starting a peer never retries.
    """
    for attempt in range(SSH_RETRIES + 1 if retry and not host.local else 1):
        r = subprocess.run(shell_argv(host, script), capture_output=True, text=True)
        if r.returncode != SSH_FAIL or host.local:
            break
        log.warning("%s: SSH failed (%s); retry %d/%d", host.name, r.stderr.strip()[-120:],
                    attempt + 1, SSH_RETRIES)  # fmt: skip
        time.sleep(SSH_RETRY_WAIT_S)
    if check and r.returncode != 0:
        raise RuntimeError(f"{host.name}: {script!r} failed: {r.stderr.strip()}")
    return r.stdout


def resolve(host: Host, remote_repo: str) -> None:
    host.home = sh(host, 'printf %s "$HOME"')
    host.repo = str(mr.REPO) if host.local else f"{host.home}/{remote_repo}"


def fetch(host: Host, remote: str, local: Path) -> None:
    """Copy one file from host to the driver (no-op when the driver's own peer already wrote it
    there: with the default --data the local host's run folder IS its collection folder)."""
    local.parent.mkdir(parents=True, exist_ok=True)
    if host.local:
        if Path(remote).resolve() != local.resolve():
            shutil.copyfile(remote, local)
    else:
        subprocess.run(["scp", "-q", *SSH_OPTS, f"{host.target}:{remote}", str(local)],
                       check=True)  # fmt: skip


def parse_synced(log_text: str) -> set[str]:
    return set(_SYNCED_RE.findall(log_text))


def parse_stat(out: str) -> tuple[int, int, float] | None:
    """`inode size now` printed by the poll script, or None when the file is not there yet."""
    parts = out.split()
    if len(parts) != 3 or parts[0] == "-":
        return None
    return int(parts[0]), int(parts[1]), float(parts[2])


# ---------------------------------------------------------------- one mesh across the hosts


class HostMesh:
    """One peer per host, each a fresh sync folder; every peer lists the others statically."""

    def __init__(self, hosts: list[Host], args: argparse.Namespace, exp: str, run: int,
                 tag: str, chunking: str = "cdc", tls: bool = False) -> None:  # fmt: skip
        self.hosts, self.args, self.chunking, self.tls = hosts, args, chunking, tls
        self.rel = f"data/raw/hosts/{exp}/run{run}/{tag}"
        self.raw = args.data / "raw" / "hosts" / exp / f"run{run}" / tag

    def base(self, h: Host) -> str:
        return f"{h.repo}/{self.rel}/{h.name}"

    def data(self, h: Host) -> str:
        return f"{self.base(h)}/data"

    def prepare(self, h: Host) -> None:
        b = shlex.quote(self.base(h))
        sh(h, f"rm -rf {b} && mkdir -p {b}/data/{META_DIR} && : > {b}/empty.toml")

    def start_script(self, h: Host) -> str:
        a, b = self.args, self.base(h)
        statics = " ".join(f"--peer {o.ip}:{o.port}" for o in self.hosts if o is not h)
        if self.tls:
            mode = f"--tls --cert-dir {shlex.quote(h.home + '/' + a.cert_dir)}"
        else:
            mode = "--plain"
        cmd = (f".venv/bin/python scripts/measure/peer.py {b}/metrics.csv -- --dir {b}/data "
               f"--name {h.name} --secret-file {shlex.quote(h.home + '/' + a.secret)} "
               f"--config {b}/empty.toml {mode} --chunking {self.chunking} "
               f"--tcp-port {h.port} --udp-port {a.udp_port} {statics}")  # fmt: skip
        return (
            f"cd {shlex.quote(h.repo)} && python3 -c 'import time; print(time.time())' && "
            f"(setsid nohup {cmd} > {b}/peer.log 2>&1 < /dev/null & echo $! > {b}/pid)"
        )

    def start(self, h: Host) -> float:
        """Start h's peer; returns the start time on h's clock."""
        return float(sh(h, self.start_script(h)).split()[0])

    def log_text(self, h: Host) -> str:
        return sh(
            h, f"cat {shlex.quote(self.base(h) + '/peer.log')} 2>/dev/null || true", retry=True
        )

    def wait(self, cond: Callable[[], bool], what: str) -> None:
        end = time.monotonic() + self.args.timeout
        while not cond():
            if time.monotonic() > end:
                raise TimeoutError(f"timed out after {self.args.timeout:.0f} s waiting for {what}")
            time.sleep(self.args.poll)

    def connected(self, h: Host, others: list[Host]) -> bool:
        return {o.name for o in others} <= parse_synced(self.log_text(h))

    def wait_connected(self, hosts: list[Host]) -> None:
        for h in hosts:
            others = [o for o in hosts if o is not h]
            self.wait(lambda h=h, o=others: self.connected(h, o), f"{h.name} links")

    def place(self, h: Host, rel: str) -> float:
        """Stage then rename the test file into h's sync folder; time on h's clock."""
        src = shlex.quote(f"{h.repo}/{self.args.testdata_rel}/{rel}")
        stage = shlex.quote(f"{self.data(h)}/{META_DIR}/measure-stage")
        dest = shlex.quote(f"{self.data(h)}/{mr.TARGET}")
        out = sh(h, f"cp {src} {stage} && mv {stage} {dest} && "
                    "python3 -c 'import time; print(time.time())'")  # fmt: skip
        return float(out.split()[0])

    def poll_script(self, h: Host) -> str:
        f = shlex.quote(f"{self.data(h)}/{mr.TARGET}")
        return (
            f"s=$(stat -c '%i %s' {f} 2>/dev/null || echo '- -'); "
            "echo \"$s $(python3 -c 'import time; print(time.time())')\""
        )

    def stat(self, h: Host) -> tuple[int, int, float] | None:
        return parse_stat(sh(h, self.poll_script(h), retry=True))

    def wait_files(self, receivers: list[Host], size: int,
                   before: dict[str, int | None]) -> dict[str, float]:  # fmt: skip
        """Receiver-clock time each receiver's TARGET is replaced by a file of `size`."""
        done: dict[str, float] = {}
        end = time.monotonic() + self.args.timeout
        while len(done) < len(receivers):
            for h in receivers:
                if h.name in done:
                    continue
                st = self.stat(h)
                if st and st[0] != before.get(h.name) and st[1] == size:
                    done[h.name] = st[2]
            if time.monotonic() > end:
                missing = [h.name for h in receivers if h.name not in done]
                raise TimeoutError(f"{missing} did not get the file in {self.args.timeout:.0f} s")
            time.sleep(self.args.poll)
        return done

    def sha(self, h: Host, path: str) -> str:
        return sh(h, f"sha256sum {shlex.quote(path)}", retry=True).split()[0]

    def verify(self, receivers: list[Host], sha: str) -> None:
        for h in receivers:
            got = self.sha(h, f"{self.data(h)}/{mr.TARGET}")
            if got != sha:
                raise RuntimeError(f"{h.name}: {mr.TARGET} has sha256 {got}, expected {sha}")

    def indexed(self, h: Host, sha: str) -> bool:
        idx = shlex.quote(f"{self.data(h)}/{META_DIR}/index.json")
        out = sh(h, f"cat {idx} 2>/dev/null || true", retry=True)
        try:
            entries = json.loads(out).get("entries", [])
        except ValueError:
            return False
        return any(e.get("path") == mr.TARGET and e.get("file_hash") == sha for e in entries)

    def stop(self) -> None:
        """SIGINT every peer (clean BYE), SIGKILL after STOP_WAIT_S; then collect raw files."""
        for h in self.hosts:
            pid = shlex.quote(f"{self.base(h)}/pid")
            sh(h, f"p=$(cat {pid} 2>/dev/null) || exit 0; kill -INT $p 2>/dev/null; "
                  f"for i in $(seq {STOP_WAIT_S * 10}); do kill -0 $p 2>/dev/null || exit 0; "
                  "sleep 0.1; done; kill -9 $p 2>/dev/null; true", check=False)  # fmt: skip
        for h in self.hosts:
            for name in ("metrics.csv", "peer.log"):
                try:
                    fetch(h, f"{self.base(h)}/{name}", self.raw / h.name / name)
                except (subprocess.CalledProcessError, FileNotFoundError) as e:
                    log.warning("%s: could not collect %s: %s", h.name, name, e)
            if not self.args.keep_data:
                sh(h, f"rm -rf {shlex.quote(self.data(h))}", check=False)

    def metrics(self, h: Host) -> list[mr.Msg]:
        return mr.read_metrics(self.raw / h.name / "metrics.csv")

    def __enter__(self) -> HostMesh:
        for h in self.hosts:
            self.prepare(h)
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


# ---------------------------------------------------------------- single-clock rows


def first_recv(msgs: list[mr.Msg], type_: str, t0: float, t1: float) -> float | None:
    ts = [m.t for m in msgs if m.direction == "recv" and m.type == type_ and t0 <= m.t <= t1]
    return min(ts) if ts else None


def last_recv(msgs: list[mr.Msg], type_: str, t0: float, t1: float) -> float | None:
    ts = [m.t for m in msgs if m.direction == "recv" and m.type == type_ and t0 <= m.t <= t1]
    return max(ts) if ts else None


def row(step: mr.Step, receiver: str, rmsgs: list[mr.Msg], r_t0: float, r_t1: float,
        size: int, notify_s: float | None, poll: float, start: float | None = None
        ) -> dict[str, str]:  # fmt: skip
    """One receiver's row. Every time is a difference of two readings of ONE host's clock:
    r_t0/r_t1/start are on the receiver's clock (they bound its metrics window)."""
    w = mr.window(rmsgs, r_t0, r_t1)
    last = last_recv(rmsgs, "CHUNK_DATA", r_t0, r_t1)
    if start is not None:  # sources: joiner_start -> last chunk
        e2e, frm = (None if last is None else last - start), "joiner_start"
    else:
        first = first_recv(rmsgs, "NOTIFY", r_t0, r_t1)
        e2e = None if first is None or last is None else max(0.0, last - first)
        frm = "first_notify"
    return {
        "experiment": step.experiment, "variant": step.variant, "run": str(step.run),
        "receiver": receiver, "file": step.file, "size": str(size),
        "e2e_s": mr.fmt(e2e), "notify_s": mr.fmt(notify_s), "transfer_s": mr.fmt(w.transfer_s),
        "chunk_msgs": str(w.chunk_msgs), "chunk_bytes": str(w.chunk_bytes),
        "recv_bytes": str(w.recv_bytes), "sent_bytes": str(w.sent_bytes),
        "sources": str(len(w.source_bytes)),
        "source_bytes": ";".join(f"{k}:{v}" for k, v in sorted(w.source_bytes.items())),
        "e2e_from": frm, "poll_s": mr.fmt(poll), "suspect": mr.suspect(w.chunk_bytes, size),
    }  # fmt: skip


@dataclass
class Pending:
    """A measured step whose rows are computed after the peers stop and CSVs are collected."""

    step: mr.Step
    src: Host
    receivers: list[Host]
    s_t0: float
    r_t0: dict[str, float]
    done: dict[str, float]
    size: int
    start: dict[str, float] | None = None


def rows_of(mesh: HostMesh, pend: list[Pending]) -> list[dict[str, str]]:
    out = []
    for p in pend:
        smsgs = mesh.metrics(p.src)
        sw = mr.window(smsgs, p.s_t0, p.s_t0 + mesh.args.timeout)
        notify_s = None if sw.first_notify is None else sw.first_notify - p.s_t0
        for h in p.receivers:
            r = row(p.step, h.name, mesh.metrics(h), p.r_t0[h.name], p.done[h.name], p.size,
                    None if p.start else notify_s, mesh.args.poll,
                    p.start[h.name] if p.start else None)  # fmt: skip
            if r["suspect"]:
                log.warning("%s/%s run %s %s: %s chunk bytes for a %s-byte file (dup, #60)",
                            r["experiment"], r["variant"], r["run"], r["receiver"],
                            r["chunk_bytes"], r["size"])  # fmt: skip
            log.info("%s/%s run %s %s: e2e %ss transfer %ss %s chunk bytes from %s",
                     r["experiment"], r["variant"], r["run"], r["receiver"], r["e2e_s"][:6],
                     r["transfer_s"][:6], r["chunk_bytes"], r["source_bytes"])  # fmt: skip
            out.append(r)
    return out


def measure(mesh: HostMesh, step: mr.Step, src: Host, receivers: list[Host],
            manifest: dict[str, str], sizes: dict[str, int]) -> Pending:  # fmt: skip
    size = sizes[step.file]
    before, r_t0 = {}, {}
    for h in receivers:
        st = mesh.stat(h)
        before[h.name] = st[0] if st else None
        now = sh(h, "python3 -c 'import time; print(time.time())'", retry=True)
        r_t0[h.name] = float(now.split()[0])
    s_t0 = mesh.place(src, step.file)
    done = mesh.wait_files(receivers, size, before)
    mesh.verify(receivers, manifest[step.file])
    return Pending(step, src, receivers, s_t0, r_t0, done, size)


# ---------------------------------------------------------------- netem with a safe clear (cond. 5)


def keep_sudo_alive(stop: threading.Event, interval: float = SUDO_KEEPALIVE_S) -> None:
    """Refresh the cached sudo credential until stop is set (runs in a daemon thread).

    A condition-5 batch lasts far longer than sudo's cache; without this the final netem clear
    failed on the real 3-host run and left the source's Wi-Fi impaired (#91).
    """
    while not stop.wait(interval):
        r = subprocess.run(["sudo", "-n", "-v"], capture_output=True)
        if r.returncode != 0:
            log.warning("could not refresh sudo; netem clear will ask for the password")


def clear_netem(iface: str) -> None:
    """Clear netem on iface; never leave the link impaired. Non-interactive first (sudo -n),
    then an interactive sudo (asks for the password) if the cached credential has expired."""
    try:
        subprocess.run(mr.netem_cmd("clear", iface), check=True)
        return
    except subprocess.CalledProcessError:
        if os.geteuid() == 0:
            raise
        log.error("netem clear on %s failed without a password; asking sudo interactively", iface)
    try:
        subprocess.run(["sudo", str(mr.NETEM_SH), "clear", iface], check=True)
    except subprocess.CalledProcessError:
        log.critical("netem may still be ON: run  sudo %s clear %s", mr.NETEM_SH, iface)
        raise


@contextmanager
def netem_link(iface: str, profile: list[str] | None) -> Iterator[None]:
    """Impair iface for the block; always cleared afterwards, even on errors (cond. 5)."""
    clear_netem(iface)
    try:
        if profile is not None:
            subprocess.run(mr.netem_cmd("apply", iface, *profile), check=True)
            log.info("netem on %s: %s", iface, " ".join(profile))
        yield
    finally:
        clear_netem(iface)


# ---------------------------------------------------------------- experiments (§11)


def start_all(mesh: HostMesh, hosts: list[Host]) -> dict[str, float]:
    starts = {h.name: mesh.start(h) for h in hosts}
    mesh.wait_connected(hosts)
    return starts


def exp_full_delta(run: int, hosts: list[Host], args: argparse.Namespace,
                   manifest: dict[str, str], sizes: dict[str, int]) -> list[dict]:  # fmt: skip
    """§11 condition 1: A in full, then its 1 KiB overwrite (delta)."""
    with HostMesh(hosts, args, "full_delta", run, "cdc") as mesh:
        src, *rcv = hosts
        start_all(mesh, hosts)
        pend = [measure(mesh, mr.Step("full_delta", "full", run, mr.A), src, rcv, manifest, sizes),
                measure(mesh, mr.Step("full_delta", "delta", run, mr.OVERWRITE), src, rcv,
                        manifest, sizes)]  # fmt: skip
    return rows_of(mesh, pend)


def exp_fixed_cdc(run: int, hosts: list[Host], args: argparse.Namespace,
                  manifest: dict[str, str], sizes: dict[str, int]) -> list[dict]:  # fmt: skip
    """§11 condition 2 (D10): 1 KiB inserted at offset 0, fixed vs content-defined chunking."""
    rows: list[dict] = []
    for mode in ("fixed", "cdc"):
        with HostMesh(hosts, args, "fixed_cdc", run, mode, chunking=mode) as mesh:
            src, *rcv = hosts
            start_all(mesh, hosts)
            measure(mesh, mr.Step("fixed_cdc", f"{mode}-base", run, mr.A), src, rcv, manifest,
                    sizes)  # fmt: skip
            pend = [measure(mesh, mr.Step("fixed_cdc", mode, run, mr.INSERT), src, rcv,
                            manifest, sizes)]  # fmt: skip
        rows += rows_of(mesh, pend)
    return rows


def exp_sources(run: int, hosts: list[Host], args: argparse.Namespace,
                manifest: dict[str, str], sizes: dict[str, int]) -> list[dict]:  # fmt: skip
    """§11 condition 3: the joiner (last host) fetches A from one vs two holders."""
    rows: list[dict] = []
    joiner = hosts[-1]
    for n in (1, 2):
        holders = hosts[:n]
        if n >= len(hosts):
            raise ValueError("sources needs at least 3 hosts for the 2-source variant")
        variant = f"{n}src"
        for attempt in range(1, args.retries + 2):
            with HostMesh([*holders, joiner], args, "sources", run, variant) as mesh:
                for h in holders:
                    mesh.place(h, mr.A)
                start_all(mesh, holders)
                for h in holders:
                    mesh.wait(lambda h=h: mesh.indexed(h, manifest[mr.A]), f"{h.name} index")
                start = mesh.start(joiner)
                done = mesh.wait_files([joiner], sizes[mr.A], {joiner.name: None})
                mesh.verify([joiner], manifest[mr.A])
                pend = Pending(mr.Step("sources", variant, run, mr.A), holders[0], [joiner],
                               0.0, {joiner.name: start}, done, sizes[mr.A],
                               {joiner.name: start})  # fmt: skip
            got = rows_of(mesh, [pend])
            if int(got[0]["sources"]) == n and not got[0]["suspect"]:
                rows += got
                break
            dup = " (duplicates)" if got[0]["suspect"] else ""
            log.warning("%s run %d attempt %d: %s source(s)%s; retrying",
                        variant, run, attempt, got[0]["sources"], dup)  # fmt: skip
        else:
            raise RuntimeError(f"{variant} run {run}: no clean {n}-source attempt")
    return rows


def exp_impaired(run: int, hosts: list[Host], args: argparse.Namespace,
                 manifest: dict[str, str], sizes: dict[str, int]) -> list[dict]:  # fmt: skip
    """§11 condition 5: A clean, with netem loss, with netem delay on the source's --iface.

    netem shapes only packets LEAVING the source, so loss/delay hit the data in one direction:
    a real link, unlike the single-host pilot on lo (both directions, #79).
    """
    if not hosts[0].local:
        raise ValueError("impaired: the source (first host) must be this machine (netem, sudo)")
    rows: list[dict] = []
    for variant, profile in mr.impaired_profiles(args):
        with HostMesh(hosts, args, "impaired", run, variant) as mesh:
            src, *rcv = hosts
            start_all(mesh, hosts)
            with netem_link(args.iface, profile):
                pend = [measure(mesh, mr.Step("impaired", variant, run, mr.A), src, rcv,
                                manifest, sizes)]  # fmt: skip
        rows += rows_of(mesh, pend)
    return rows


def exp_tls(run: int, hosts: list[Host], args: argparse.Namespace,
            manifest: dict[str, str], sizes: dict[str, int]) -> list[dict]:  # fmt: skip
    """§11 condition 6: plain vs TLS; setup_s from each receiver's own log (one clock)."""
    rows: list[dict] = []
    for mode in ("plain", "tls"):
        with HostMesh(hosts, args, "tls", run, mode, tls=mode == "tls") as mesh:
            src, *rcv = hosts
            mesh.start(src)
            up = [src]
            for r in rcv:  # each receiver starts alone and dials the peers already up
                mesh.start(r)
                mesh.wait(lambda r=r, u=list(up): mesh.connected(r, u), f"{r.name} links")
                up.append(r)
            mesh.wait_connected(up)
            pend = [measure(mesh, mr.Step("tls", mode, run, mr.A), src, rcv, manifest, sizes)]
        got = rows_of(mesh, pend)
        for r_row, r in zip(got, rcv, strict=True):
            text = (mesh.raw / r.name / "peer.log").read_text(errors="replace")
            r_row["setup_s"] = mr.fmt(mr.setup_time(text, src.name))
        rows += got
    return rows


EXPERIMENTS = {
    "full_delta": exp_full_delta,
    "fixed_cdc": exp_fixed_cdc,
    "sources": exp_sources,
    "impaired": exp_impaired,
    "tls": exp_tls,
}


# ---------------------------------------------------------------- main


def needs_password(host: Host) -> bool:
    """True if a fresh SSH login to host would stop to ask for a password (no key set up)."""
    if host.local:
        return False
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ControlPath=none",
                        "-o", "ConnectTimeout=10", host.target, "true"],
                       capture_output=True)  # fmt: skip
    return r.returncode != 0


def runs_to_do(folder: Path, runs: int, resume: bool) -> list[int]:
    """Run numbers to execute: all of 1..runs, or with --resume only those without a CSV."""
    if not resume:
        return list(range(1, runs + 1))
    return [k for k in range(1, runs + 1) if not (folder / f"run{k}.csv").exists()]


def check_testdata(hosts: list[Host], args: argparse.Namespace,
                   manifest: dict[str, str]) -> None:  # fmt: skip
    """Every host's copy of the files a run places must match the driver's manifest."""
    for h in hosts:
        for rel in (mr.A, mr.OVERWRITE, mr.INSERT):
            path = f"{h.repo}/{args.testdata_rel}/{rel}"
            got = sh(h, f"sha256sum {shlex.quote(path)} 2>/dev/null || true").split()
            if not got or got[0] != manifest[rel]:
                raise RuntimeError(f"{h.name}: {path} missing or different; run "
                                   "scripts/gen_testdata.py testdata --sets A,D there")  # fmt: skip


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("experiment", choices=[*EXPERIMENTS, "all"])
    p.add_argument("hosts", nargs="+", help="NAME:IP[:SSH_TARGET[:TCP_PORT]]; first = source")
    p.add_argument("--runs", type=int, default=mr.RUNS_DEFAULT, help="runs per condition (§11)")
    p.add_argument("--testdata", type=Path, default=mr.REPO / "testdata",
                   help="driver's #25 test data (its MANIFEST is the reference)")  # fmt: skip
    p.add_argument("--testdata-rel", default="testdata", help="test data dir in each repo")
    p.add_argument("--data", type=Path, default=mr.REPO / "data", help="results folder")
    p.add_argument("--remote-repo", default="meshsync", help="repo path under each remote $HOME")
    p.add_argument("--secret", default=".meshsync/key", help="secret file under each $HOME")
    p.add_argument("--cert-dir", default=".meshsync/certs", help="cert dir under each $HOME")
    p.add_argument("--udp-port", type=int, default=UDP_PORT)
    # Harness settings, not protocol values: how long and how often the driver waits.
    p.add_argument("--timeout", type=float, default=900.0, help="seconds per wait (harness)")
    p.add_argument("--poll", type=float, default=0.5, help="seconds between checks (harness)")
    p.add_argument("--keep-data", action="store_true", help="keep the hosts' sync folders")
    p.add_argument("--retries", type=int, default=3, help="extra attempts for a 2src run")
    p.add_argument(
        "--resume",
        action="store_true",
        help="keep existing data/<experiment>/run<k>.csv; run only the missing ones",
    )
    p.add_argument("--iface", default="wlp2s0", help="source interface netem impairs (cond. 5)")
    p.add_argument("--loss-pct", type=float, default=mr.LOSS_PCT_DEFAULT,
                   help="condition 5 loss %% (default 5, §10)")  # fmt: skip
    p.add_argument("--delay-ms", type=float, default=mr.DELAY_MS_DEFAULT,
                   help="condition 5 delay ms (default 50, #69)")  # fmt: skip
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    try:
        hosts = [parse_host(s) for s in args.hosts]
    except ValueError as e:
        p.error(str(e))
    if len(hosts) < 2 or len({h.name for h in hosts}) != len(hosts):
        p.error("need at least 2 hosts with distinct names")
    try:
        manifest = mr.read_manifest(args.testdata)
    except FileNotFoundError:
        p.error(f"no {args.testdata}/MANIFEST.sha256; run scripts/gen_testdata.py first")
    names = list(EXPERIMENTS) if args.experiment == "all" else [args.experiment]
    if "impaired" in names and os.geteuid() != 0:
        subprocess.run(["sudo", "-v"], check=True)  # ask once for netem (condition 5) ...
        stop = threading.Event()  # ... and keep it cached for the whole batch
        threading.Thread(target=keep_sudo_alive, args=(stop,), daemon=True).start()
    for h in hosts:
        if needs_password(h):
            log.warning(
                "%s asks for an SSH password: a dropped connection mid-batch will wait "
                "for it. For unattended runs set up a key: ssh-copy-id %s",
                h.name,
                h.target,
            )
    for h in hosts:
        resolve(h, args.remote_repo)
    check_testdata(hosts, args, manifest)
    sizes = {rel: (args.testdata / rel).stat().st_size for rel in (mr.A, mr.OVERWRITE, mr.INSERT)}

    failed: list[str] = []
    for name in names:
        if not args.resume:
            mr.clear_runs(args.data / name)
        todo = runs_to_do(args.data / name, args.runs, args.resume)
        if args.resume:
            log.info("%s: runs to do %s", name, todo or "none")
        for run in todo:
            try:
                rows = EXPERIMENTS[name](run, hosts, args, manifest, sizes)
            except Exception:  # noqa: BLE001 - one bad run must not lose the rest of the batch
                log.exception("%s run %d failed; continuing", name, run)
                failed.append(f"{name} run {run}")
                continue
            out = args.data / name / f"run{run}.csv"
            mr.write_rows(out, rows)
            log.info("wrote %s", out)
    if failed:
        log.error("%d run(s) failed: %s", len(failed), ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
