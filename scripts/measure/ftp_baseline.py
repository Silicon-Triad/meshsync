#!/usr/bin/env python3
"""FTP baseline on the same hosts as the §11 runs, for the meshsync comparison (§5). Issue #95.

Usage: scripts/measure/ftp_baseline.py SERVER CLIENT [CLIENT ...] --ftp-root DIR [--runs 5]
       SERVER: NAME:IP              (this machine: it runs the FTP server and drives the runs)
       CLIENT: NAME:IP:SSH_TARGET   (reached over SSH, as in scripts/measure/hosts.py)
  e.g. scripts/measure/ftp_baseline.py kratos:192.168.100.92 --ftp-root /srv/ftptest \
         minipc:192.168.100.190:user@192.168.100.190 defalt:192.168.100.80:user@192.168.100.80
       scripts/measure/ftp_baseline.py get SERVER_IP USER PASS_FILE NAME SHA256   (client side)

The server (e.g. vsftpd) is set up by hand, with a throwaway FTP user whose home is --ftp-root
and is writable by the driver. Every run places a file there, then all clients download it at
the same time with Python's ftplib (standard library, D15):
  full       set A, the 100 MiB file                     (compare full_delta "full")
  overwrite  set D, A with 1 KiB overwritten              (compare full_delta "delta")
  insert     set D, A with 1 KiB inserted at offset 0     (compare fixed_cdc "cdc")
  loss       set A with netem loss on the server's --iface (compare impaired "loss")
FTP has no delta transfer, so an edited file is downloaded whole: that is what this measures.

Each client times its own download (one clock per number, as in #77) and checks the SHA-256
against the driver's manifest, because FTP itself has no integrity check. The FTP password is
read from a file on each client (--pass-file, never on a command line) and is sent in plain
text by FTP: use a throwaway account only.

Output: data/ftp/run<k>.csv in run.py's columns, so analysis/analyze.py reads it:
  transfer_s   RETR sent -> last byte received          e2e_s   connect -> last byte received
  chunk_bytes  file bytes received (FTP payload)        sources 1 (FTP is client-server)
Writes nothing under src/ and changes no meshsync behaviour.
Owner: Huzaifa Hanif (CS-24039); written for #95 by Mujtaba Jawaid Rao.
"""

from __future__ import annotations

import argparse
import ftplib
import hashlib
import json
import logging
import os
import shlex
import shutil
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hosts as hs  # noqa: E402 - sibling script: SSH hosts, netem with sudo keep-alive, resume
import run as mr  # noqa: E402 - sibling script: row format, test data set, netem defaults

log = logging.getLogger("ftp_baseline")

EXPERIMENT = "ftp"
TARGET = "large.bin"  # name of the downloaded file in the FTP root
VARIANTS: tuple[tuple[str, str, bool], ...] = (  # (variant, test data file, netem loss on)
    ("full", mr.A, False),
    ("overwrite", mr.OVERWRITE, False),
    ("insert", mr.INSERT, False),
    ("loss", mr.A, True),
)
FTP_TIMEOUT_S = 60.0  # harness: socket timeout for one FTP operation, not a protocol value
BLOCK = 1 << 16  # harness: ftplib read size


# ---------------------------------------------------------------- client side (runs on a client)


def download(server: str, user: str, password: str, name: str,
             ftp_factory: Callable[[], ftplib.FTP] = ftplib.FTP) -> dict:  # fmt: skip
    """Download name from server in binary mode, keeping only its hash; times on this clock."""
    sha = hashlib.sha256()
    got = 0

    def block(data: bytes) -> None:
        nonlocal got
        got += len(data)
        sha.update(data)

    t_connect = time.time()
    ftp = ftp_factory()
    try:
        ftp.connect(server, timeout=FTP_TIMEOUT_S)
        ftp.login(user, password)
        t_retr = time.time()
        ftp.retrbinary(f"RETR {name}", block, blocksize=BLOCK)
        t_end = time.time()
        ftp.quit()
    finally:
        ftp.close()
    return {"bytes": got, "sha256": sha.hexdigest(), "e2e_s": t_end - t_connect,
            "transfer_s": t_end - t_retr}  # fmt: skip


def client_main(args: argparse.Namespace) -> int:
    password = Path(args.pass_file).expanduser().read_text().strip()
    r = download(args.server, args.user, password, args.name)
    r["ok"] = r["sha256"] == args.sha256
    print(json.dumps(r))
    return 0 if r["ok"] else 1


# ---------------------------------------------------------------- driver side (runs on the server)


def place(src: Path, root: Path) -> None:
    """Put src at root/TARGET atomically, so a client never downloads a half-copied file."""
    tmp = root / f".{TARGET}.tmp"
    shutil.copyfile(src, tmp)
    os.replace(tmp, root / TARGET)


def client_script(host: hs.Host, args: argparse.Namespace, server_ip: str, sha: str) -> str:
    me = f"{host.repo}/scripts/measure/ftp_baseline.py"
    return (f"cd {shlex.quote(host.repo)} && .venv/bin/python {shlex.quote(me)} get "
            f"{shlex.quote(server_ip)} {shlex.quote(args.user)} {shlex.quote(args.pass_file)} "
            f"{TARGET} {sha}")  # fmt: skip


def row(variant: str, run: int, receiver: str, rel: str, size: int, server: str,
        r: dict) -> dict:  # fmt: skip
    return {
        "experiment": EXPERIMENT,
        "variant": variant,
        "run": str(run),
        "receiver": receiver,
        "file": rel,
        "size": str(size),
        "e2e_s": mr.fmt(r["e2e_s"]),
        "notify_s": "",
        "transfer_s": mr.fmt(r["transfer_s"]),
        "chunk_msgs": "",
        "chunk_bytes": str(r["bytes"]),
        "recv_bytes": str(r["bytes"]),
        "sent_bytes": "",
        "sources": "1",
        "source_bytes": f"{server}:{r['bytes']}",
        "e2e_from": "ftp_connect",
        "poll_s": "",
        "suspect": "",
        "setup_s": "",
    }


def parse_result(host: str, out: str) -> dict:
    """The client's last output line is its JSON result; anything else is an error."""
    lines = out.strip().splitlines()
    if not lines:
        raise RuntimeError(f"{host}: no result from the FTP client")
    r = json.loads(lines[-1])
    if not r.get("ok"):
        raise RuntimeError(f"{host}: downloaded file has the wrong SHA-256 (FTP has no check)")
    return r


def fetch_all(clients: list[hs.Host], args: argparse.Namespace, server: hs.Host,
              sha: str) -> dict[str, dict]:  # fmt: skip
    """Every client downloads at the same time; results by client name."""
    results: dict[str, dict] = {}
    errors: list[str] = []

    def one(h: hs.Host) -> None:
        try:
            results[h.name] = parse_result(h.name, hs.sh(h, client_script(h, args, server.ip, sha)))
        except Exception as e:  # noqa: BLE001 - reported below for every client
            errors.append(f"{h.name}: {e}")

    threads = [threading.Thread(target=one, args=(h,)) for h in clients]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if errors:
        raise RuntimeError("; ".join(errors))
    return results


def measure_run(run: int, server: hs.Host, clients: list[hs.Host], args: argparse.Namespace,
                manifest: dict[str, str]) -> list[dict]:  # fmt: skip
    rows = []
    for variant, rel, loss in VARIANTS:
        src = args.testdata / rel
        place(src, args.ftp_root)
        profile = ["loss", f"{args.loss_pct:g}"] if loss else None
        with hs.netem_link(args.iface, profile):
            results = fetch_all(clients, args, server, manifest[rel])
        for h in clients:
            r = results[h.name]
            rows.append(row(variant, run, h.name, rel, src.stat().st_size, server.name, r))
            log.info("%s/%s run %d %s: %.2fs, %d bytes", EXPERIMENT, variant, run, h.name,
                     r["transfer_s"], r["bytes"])  # fmt: skip
    return rows


def driver_main(args: argparse.Namespace, p: argparse.ArgumentParser) -> int:
    try:
        server = hs.parse_host(args.server)
        clients = [hs.parse_host(c) for c in args.clients]
    except ValueError as e:
        p.error(str(e))
    if not server.local:
        p.error("the server must be this machine (NAME:IP): it places the files and runs netem")
    if not clients or any(c.local for c in clients):
        p.error("every client needs an SSH target (NAME:IP:SSH_TARGET)")
    if len({h.name for h in [server, *clients]}) != len(clients) + 1:
        p.error("host names must be distinct")
    if not args.ftp_root.is_dir() or not os.access(args.ftp_root, os.W_OK):
        p.error(f"--ftp-root {args.ftp_root} must be a folder this user can write")
    try:
        manifest = mr.read_manifest(args.testdata)
    except (OSError, ValueError) as e:
        p.error(f"test data: {e}; run scripts/gen_testdata.py testdata --sets A,D")
    for h in clients:
        hs.resolve(h, args.remote_repo)

    hs.subprocess.run(["sudo", "-v"], check=True)  # once, for netem in the loss variant
    stop = threading.Event()
    threading.Thread(target=hs.keep_sudo_alive, args=(stop,), daemon=True).start()
    folder = args.data / EXPERIMENT
    if not args.resume:
        mr.clear_runs(folder)
        (folder / hs.RUNS_LOG).unlink(missing_ok=True)
    todo = hs.runs_to_do(folder, args.runs, args.resume)
    log.info("%s: runs to do %s", EXPERIMENT, todo or "none")
    failed = []
    try:
        for run in todo:
            start = time.time()
            try:
                rows = measure_run(run, server, clients, args, manifest)
            except Exception:  # noqa: BLE001 - one bad run must not lose the rest of the batch
                log.exception("%s run %d failed; continuing", EXPERIMENT, run)
                failed.append(run)
                continue
            out = folder / f"run{run}.csv"
            mr.write_rows(out, rows)
            hs.log_run(folder, run, start, time.time())
            log.info("wrote %s", out)
    finally:
        stop.set()
        (args.ftp_root / TARGET).unlink(missing_ok=True)
    if failed:
        log.error("%d run(s) failed: %s (rerun with --resume)", len(failed), failed)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["get"]:
        c = argparse.ArgumentParser(description="FTP client side of ftp_baseline.py")
        for name in ("server", "user", "pass_file", "name", "sha256"):
            c.add_argument(name)
        return client_main(c.parse_args(argv[1:]))
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("server", help="NAME:IP of this machine (runs the FTP server)")
    p.add_argument("clients", nargs="+", help="NAME:IP:SSH_TARGET")
    p.add_argument("--ftp-root", type=Path, required=True, help="FTP user's home, writable here")
    p.add_argument("--user", default="ftptest", help="throwaway FTP user")
    p.add_argument("--pass-file", default="~/.meshsync/ftp.pass", help="password file on clients")
    p.add_argument("--runs", type=int, default=mr.RUNS_DEFAULT, help="runs (§11)")
    p.add_argument("--testdata", type=Path, default=mr.REPO / "testdata", help="#25 test data")
    p.add_argument("--data", type=Path, default=mr.REPO / "data", help="results folder")
    p.add_argument("--remote-repo", default="meshsync", help="repo path under each client $HOME")
    p.add_argument("--iface", default="wlp2s0", help="server interface netem impairs (loss)")
    p.add_argument("--loss-pct", type=float, default=mr.LOSS_PCT_DEFAULT,
                   help="loss %% for the loss variant (default 5, §10)")  # fmt: skip
    p.add_argument("--resume", action="store_true", help="run only the missing run<k>.csv")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    return driver_main(args, p)


if __name__ == "__main__":
    sys.exit(main())
