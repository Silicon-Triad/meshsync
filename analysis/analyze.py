#!/usr/bin/env python3
"""Tables and graphs from the §11 measurement CSVs (PROJECT_CONTEXT.md §11, R6). Issue #71.

Usage: analysis/analyze.py [--data data] [--out analysis/out] [--no-plots]

Reads every data/<experiment>/run<k>.csv written by scripts/measure/run.py (#20, #69) and writes
to --out:
  <experiment>.md / .csv   mean ± sample std dev and n per variant and metric (table for the report)
  <experiment>.pdf / .png  one bar chart with ±1 sd error bars for the experiment's comparison
  savings.md               §13 bytes saved: delta vs full copy, CDC vs fixed after an insert,
                           counted twice: chunk data only, and every byte received (recv_bytes:
                           NOTIFY with all chunk hashes, requests, PING/PONG as well)

Statistics: a run's receivers share one mesh and one set of conditions, so they are averaged
first; mean and sd are then taken over runs (n = runs, 5 per §11). Rows flagged suspect="dup"
(chunks downloaded twice, #60) are dropped and counted, never silently used.
Throughput = chunk_bytes / transfer_s, in MB/s (10^6 bytes); undefined when transfer_s is 0.

Plots need matplotlib (D15 analysis dependency: pip install -e .[analysis]); tables need only
the standard library. Owner: Huzaifa Hanif (CS-24039).
"""

from __future__ import annotations

import argparse
import csv
import logging
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("analysis")

REPO = Path(__file__).resolve().parents[1]

# metric -> (column or derived name, label for tables and axes)
METRICS: dict[str, str] = {
    "transfer_s": "transfer time (s)",
    "e2e_s": "end-to-end time (s)",
    "chunk_bytes": "chunk bytes received",
    "recv_bytes": "total bytes received",
    "throughput_mbps": "throughput (MB/s)",
    "setup_s": "setup time (s)",
}
# The comparison each experiment's graph shows (§11 conditions; unknown experiments: transfer).
PLOTS: dict[str, list[str]] = {
    "full_delta": ["chunk_bytes"],  # condition 1: bytes sent, full vs delta
    "fixed_cdc": ["chunk_bytes"],  # condition 2 (D10): bytes re-sent after an insert
    "sources": ["transfer_s"],  # condition 3
    "impaired": ["transfer_s"],  # condition 5
    "tls": ["setup_s", "throughput_mbps"],  # condition 6 (§12 security vs performance)
}
LOG_SCALE = {"chunk_bytes"}  # delta / CDC values are orders of magnitude below full copies


@dataclass(frozen=True)
class Stat:
    experiment: str
    variant: str
    metric: str
    n: int
    mean: float
    sd: float | None  # None when n < 2


def _num(x: str | None) -> float | None:
    if x is None or x.strip() == "":
        return None
    return float(x)


def row_metrics(row: dict[str, str]) -> dict[str, float | None]:
    """The metric values of one CSV row (one receiver in one run)."""
    out = {m: _num(row.get(m)) for m in METRICS if m != "throughput_mbps"}
    t, b = out["transfer_s"], out["chunk_bytes"]
    out["throughput_mbps"] = b / t / 1e6 if t and b is not None else None
    return out


def load(data: Path) -> tuple[dict[str, list[dict[str, str]]], dict[str, int]]:
    """{experiment: rows} from data/<experiment>/run*.csv, and dropped "dup" rows per experiment."""
    rows: dict[str, list[dict[str, str]]] = {}
    dropped: dict[str, int] = {}
    for path in sorted(data.glob("*/run*.csv")):
        exp = path.parent.name
        with path.open(newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("suspect"):
                    dropped[exp] = dropped.get(exp, 0) + 1
                    continue
                rows.setdefault(exp, []).append(r)
    return rows, dropped


def summarize(experiment: str, rows: list[dict[str, str]]) -> list[Stat]:
    """Mean ± sd over runs per variant and metric; receivers within a run are averaged first."""
    # (variant, metric) -> run -> values from that run's receivers
    per_run: dict[tuple[str, str], dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    order: list[str] = []
    for r in rows:
        v = r["variant"]
        if v not in order:
            order.append(v)
        for m, x in row_metrics(r).items():
            if x is not None:
                per_run[(v, m)][r["run"]].append(x)
    stats = []
    for v in order:
        for m in METRICS:
            runs = per_run.get((v, m))
            if not runs:
                continue
            means = [statistics.fmean(xs) for xs in runs.values()]
            sd = statistics.stdev(means) if len(means) > 1 else None
            stats.append(Stat(experiment, v, m, len(means), statistics.fmean(means), sd))
    return stats


def lookup(stats: list[Stat], variant: str, metric: str) -> Stat | None:
    return next((s for s in stats if s.variant == variant and s.metric == metric), None)


# What a savings row counts: chunk data only, or every application-layer byte received. For a
# small edit the NOTIFY carrying all chunk hashes can outweigh the chunk itself (#59 pilot), so
# the chunk-only figure overstates the saving on the wire (#80 review).
SAVINGS_BASIS = {"chunk_bytes": "chunk data only", "recv_bytes": "all bytes received"}


def savings(all_stats: dict[str, list[Stat]]) -> list[tuple[str, str, float, float, float]]:
    """§13: (comparison, counted, baseline bytes, new bytes, % saved) from mean bytes."""
    out = []
    for exp, base, new, label in (
        ("full_delta", "full", "delta", "delta vs full copy (1 KiB overwrite)"),
        ("fixed_cdc", "fixed", "cdc", "CDC vs fixed chunking (1 KiB insert at offset 0)"),
    ):
        for metric, counted in SAVINGS_BASIS.items():
            b = lookup(all_stats.get(exp, []), base, metric)
            n = lookup(all_stats.get(exp, []), new, metric)
            if b and n and b.mean > 0:
                out.append((label, counted, b.mean, n.mean, 100.0 * (1 - n.mean / b.mean)))
    return out


def fmt(x: float | None) -> str:
    if x is None:
        return "—"
    return f"{x:,.0f}" if abs(x) >= 1000 else f"{x:.4g}"


def write_tables(out: Path, experiment: str, stats: list[Stat], dropped: int) -> None:
    out.mkdir(parents=True, exist_ok=True)
    with (out / f"{experiment}.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["experiment", "variant", "metric", "n", "mean", "sd"])
        for s in stats:
            w.writerow(
                [s.experiment, s.variant, s.metric, s.n, s.mean, "" if s.sd is None else s.sd]
            )
    lines = [f"### {experiment}", "", "| variant | metric | n | mean ± sd |", "|---|---|---|---|"]
    for s in stats:
        lines.append(f"| {s.variant} | {METRICS[s.metric]} | {s.n} | {fmt(s.mean)} ± {fmt(s.sd)} |")
    if dropped:
        lines += ["", f"{dropped} row(s) flagged `dup` (#60) were excluded."]
    (out / f"{experiment}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_savings(out: Path, rows: list[tuple[str, str, float, float, float]]) -> None:
    lines = [
        "### Bytes saved (§13)",
        "",
        "| comparison | counted | baseline bytes | new bytes | saved |",
    ]
    lines.append("|---|---|---|---|---|")
    lines += [f"| {c} | {k} | {fmt(b)} | {fmt(n)} | {p:.2f} % |" for c, k, b, n, p in rows]
    out.mkdir(parents=True, exist_ok=True)
    (out / "savings.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def plot(out: Path, experiment: str, stats: list[Stat]) -> list[Path]:
    """Bar chart(s) of the experiment's comparison with ±1 sd error bars, as PDF and PNG."""
    import matplotlib

    matplotlib.use("Agg")  # files only, no display
    import matplotlib.pyplot as plt

    metrics = [
        m for m in PLOTS.get(experiment, ["transfer_s"]) if any(s.metric == m for s in stats)
    ]
    if not metrics:
        return []
    fig, axes = plt.subplots(1, len(metrics), figsize=(4.5 * len(metrics), 3.5), squeeze=False)
    for ax, m in zip(axes[0], metrics, strict=True):
        sel = [s for s in stats if s.metric == m]
        ax.bar(
            [s.variant for s in sel],
            [s.mean for s in sel],
            yerr=[s.sd or 0.0 for s in sel],
            capsize=4,
            color="#4C72B0",
        )
        ax.set_ylabel(METRICS[m])
        if m in LOG_SCALE:
            ax.set_yscale("log")
        n = max(s.n for s in sel)
        ax.set_title(f"{experiment} (n = {n} runs, ±1 sd)", fontsize=9)
    fig.tight_layout()
    out.mkdir(parents=True, exist_ok=True)
    paths = [out / f"{experiment}.pdf", out / f"{experiment}.png"]
    for p in paths:
        fig.savefig(p, dpi=200)
    plt.close(fig)
    return paths


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--data", type=Path, default=REPO / "data", help="folder with <experiment>/")
    p.add_argument("--out", type=Path, default=REPO / "analysis" / "out", help="output folder")
    p.add_argument("--no-plots", action="store_true", help="tables only (no matplotlib needed)")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    rows, dropped = load(args.data)
    if not rows:
        log.error("no data/<experiment>/run*.csv under %s; run scripts/measure/run.py", args.data)
        return 1
    plots = not args.no_plots
    if plots:
        try:
            import matplotlib  # noqa: F401
        except ImportError:
            log.warning("matplotlib not installed (pip install -e .[analysis]); tables only")
            plots = False
    all_stats = {}
    for exp, exp_rows in rows.items():
        stats = summarize(exp, exp_rows)
        all_stats[exp] = stats
        write_tables(args.out, exp, stats, dropped.get(exp, 0))
        if dropped.get(exp):
            log.warning("%s: excluded %d dup row(s) (#60)", exp, dropped[exp])
        if plots:
            plot(args.out, exp, stats)
        log.info("%s: %d variant(s) written to %s", exp, len({s.variant for s in stats}), args.out)
    sav = savings(all_stats)
    if sav:
        write_savings(args.out, sav)
        for c, k, _, _, pct in sav:
            log.info("saved %.2f %% (%s): %s", pct, k, c)
    return 0


if __name__ == "__main__":
    sys.exit(main())
