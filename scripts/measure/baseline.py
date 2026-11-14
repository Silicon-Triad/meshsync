#!/usr/bin/env python3
"""Summarise the network baseline for §11 / R6 (issue #70).

Usage: scripts/measure/baseline.py RAW_DIR [--out data/baseline] [--metrics NAME=CSV ...]

RAW_DIR is the output of scripts/measure/baseline.sh:
  ping_<src>_<dst>_run<k>.txt     `ping -c N` output   -> rtt.csv (one row per run)
  iperf3_<src>_<dst>_run<k>.json  `iperf3 -J` output   -> iperf3.csv (one row per run)
and summary.csv: mean ± sample sd over runs per pair, for RTT (each run's avg) and throughput
(receiver side, Mbit/s = 10^6 bit/s).

--metrics NAME=CSV (repeatable) cross-checks RTT with meshsync's own liveness PING/PONG
(PROTOCOL.md §6.7): metrics CSVs written by scripts/measure/peer.py on host NAME. Each PING sent
to a peer is paired with the next PONG received from it -> pingpong.csv (one row per pair, in
ms). The peer column there is the remote peer id as metrics.record logs it.
Owner: Huzaifa Hanif (CS-24039); written for #70 by Mujtaba Jawaid Rao.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("baseline")

_NAME_RE = re.compile(r"^(ping|iperf3)_(.+?)_(.+?)_run(\d+)\.(txt|json)$")
_RTT_RE = re.compile(r"= ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+) ms")  # min/avg/max/mdev (iputils)
_LOSS_RE = re.compile(r"([\d.]+)% packet loss")


@dataclass(frozen=True)
class PingRun:
    src: str
    dst: str
    run: int
    min_ms: float | None
    avg_ms: float | None
    max_ms: float | None
    mdev_ms: float | None
    loss_pct: float


@dataclass(frozen=True)
class IperfRun:
    src: str
    dst: str
    run: int
    mbps: float
    retransmits: int | None


def parse_ping(text: str) -> tuple[float | None, ...]:
    """(min, avg, max, mdev, loss %) from iputils ping output; RTTs None if every ping was lost."""
    loss = _LOSS_RE.search(text)
    if not loss:
        raise ValueError("no packet loss line: not ping output")
    rtt = _RTT_RE.search(text)
    vals = tuple(float(x) for x in rtt.groups()) if rtt else (None, None, None, None)
    return (*vals, float(loss.group(1)))


def parse_iperf3(text: str) -> tuple[float, int | None]:
    """(receiver Mbit/s, sender retransmits) from `iperf3 -J` output."""
    data = json.loads(text)
    if "error" in data:
        raise ValueError(f"iperf3 error: {data['error']}")
    end = data["end"]
    mbps = end["sum_received"]["bits_per_second"] / 1e6
    return mbps, end.get("sum_sent", {}).get("retransmits")


def load(raw: Path) -> tuple[list[PingRun], list[IperfRun]]:
    pings, iperfs = [], []
    for path in sorted(raw.iterdir()):
        m = _NAME_RE.match(path.name)
        if not m:
            continue
        tool, src, dst, run = m.group(1), m.group(2), m.group(3), int(m.group(4))
        text = path.read_text(encoding="utf-8")
        try:
            if tool == "ping":
                mn, avg, mx, mdev, loss = parse_ping(text)
                pings.append(PingRun(src, dst, run, mn, avg, mx, mdev, loss))
            else:
                mbps, retr = parse_iperf3(text)
                iperfs.append(IperfRun(src, dst, run, mbps, retr))
        except (ValueError, KeyError) as e:
            log.warning("skipping %s: %s", path.name, e)
    return pings, iperfs


def mean_sd(xs: list[float]) -> tuple[float, float | None]:
    return statistics.fmean(xs), (statistics.stdev(xs) if len(xs) > 1 else None)


def summary(pings: list[PingRun], iperfs: list[IperfRun]) -> list[dict[str, str]]:
    rows = []
    for metric, runs, value in (
        ("rtt_ms", pings, lambda r: r.avg_ms),
        ("throughput_mbps", iperfs, lambda r: r.mbps),
    ):
        by_pair: dict[tuple[str, str], list[float]] = defaultdict(list)
        for r in runs:
            v = value(r)
            if v is not None:
                by_pair[(r.src, r.dst)].append(v)
        for (src, dst), xs in sorted(by_pair.items()):
            mean, sd = mean_sd(xs)
            sd_s = "" if sd is None else f"{sd:.3f}"
            rows.append({"metric": metric, "src": src, "dst": dst, "n": str(len(xs)),
                         "mean": f"{mean:.3f}", "sd": sd_s})  # fmt: skip
    return rows


def pingpong(rows: list[dict[str, str]]) -> list[tuple[str, float]]:
    """(peer, RTT ms) for each PING sent followed by a PONG received from the same peer."""
    pending: dict[str, float] = {}
    out = []
    for r in sorted(rows, key=lambda r: float(r["t"])):
        peer, t = r["peer"], float(r["t"])
        if r["direction"] == "send" and r["type"] == "PING":
            pending[peer] = t
        elif r["direction"] == "recv" and r["type"] == "PONG" and peer in pending:
            out.append((peer, (t - pending.pop(peer)) * 1000.0))
    return out


def write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def fmt(x: float | None) -> str:
    return "" if x is None else f"{x:.3f}"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("raw", type=Path, help="output folder of baseline.sh")
    p.add_argument("--out", type=Path, default=Path("data/baseline"), help="results folder")
    p.add_argument("--metrics", action="append", default=[], metavar="NAME=CSV",
                   help="peer.py metrics CSV from host NAME (PING/PONG cross-check)")  # fmt: skip
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    pings, iperfs = load(args.raw)
    if not pings and not iperfs:
        log.error("no ping_*/iperf3_* files in %s; run scripts/measure/baseline.sh", args.raw)
        return 1
    write_csv(args.out / "rtt.csv",
              ["src", "dst", "run", "min_ms", "avg_ms", "max_ms", "mdev_ms", "loss_pct"],
              [{"src": r.src, "dst": r.dst, "run": r.run, "min_ms": fmt(r.min_ms),
                "avg_ms": fmt(r.avg_ms), "max_ms": fmt(r.max_ms), "mdev_ms": fmt(r.mdev_ms),
                "loss_pct": f"{r.loss_pct:g}"} for r in pings])  # fmt: skip
    write_csv(args.out / "iperf3.csv", ["src", "dst", "run", "mbps", "retransmits"],
              [{"src": r.src, "dst": r.dst, "run": r.run, "mbps": f"{r.mbps:.3f}",
                "retransmits": "" if r.retransmits is None else r.retransmits}
               for r in iperfs])  # fmt: skip
    summ = summary(pings, iperfs)
    write_csv(args.out / "summary.csv", ["metric", "src", "dst", "n", "mean", "sd"], summ)
    for r in summ:
        log.info("%-15s %s -> %s: %s ± %s (n=%s)", r["metric"], r["src"], r["dst"], r["mean"],
                 r["sd"] or "—", r["n"])  # fmt: skip

    pp_rows = []
    for spec in args.metrics:
        host, _, path = spec.partition("=")
        if not path:
            p.error(f"--metrics needs NAME=CSV, got {spec!r}")
        with open(path, newline="", encoding="utf-8") as f:
            pairs = pingpong(list(csv.DictReader(f)))
        pp_rows += [{"host": host, "peer": peer, "rtt_ms": f"{ms:.3f}"} for peer, ms in pairs]
        for peer in sorted({peer for peer, _ in pairs}):
            xs = [ms for q, ms in pairs if q == peer]
            mean, sd = mean_sd(xs)
            log.info("PING/PONG %s -> %s: %.3f ± %s ms (n=%d)", host, peer, mean, fmt(sd) or "—",
                     len(xs))  # fmt: skip
    if pp_rows:
        write_csv(args.out / "pingpong.csv", ["host", "peer", "rtt_ms"], pp_rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
