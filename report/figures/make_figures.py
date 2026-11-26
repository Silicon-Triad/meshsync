#!/usr/bin/env python3
"""Report graphs that combine several experiments (R6): FTP vs meshsync bytes after an edit, and
application throughput against the iperf3 ceiling.

Usage: report/figures/make_figures.py   (from the repository root; needs pip install -e .[analysis])

Numbers come from analysis/analyze.py's own load() and summarize() on data/ (main batch, #77),
data/ftp/ (FTP and meshsync on the same night, #95) and data/baseline/summary.csv (#70), so they
match the report's tables. Writes report/figures/ftp-bytes.pdf and throughput-ceiling.pdf.
Owner: all members (report/). Issue #75.
"""

from __future__ import annotations

import csv
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
BLUE, ORANGE = "#4C72B0", "#DD8452"  # analyze.py's bar colour, and one for FTP


def _analyze() -> ModuleType:
    """Import analysis/analyze.py (a script, not a package) to reuse its statistics."""
    spec = importlib.util.spec_from_file_location("analyze", ROOT / "analysis" / "analyze.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["analyze"] = mod  # dataclasses need the module registered
    spec.loader.exec_module(mod)
    return mod


def _stats(an: ModuleType, data: Path) -> dict[str, list]:
    rows, _dropped = an.load(data)
    return {exp: an.summarize(exp, r) for exp, r in rows.items()}


def _get(an: ModuleType, stats: dict[str, list], exp: str, variant: str, metric: str):
    s = an.lookup(stats[exp], variant, metric)
    if s is None:
        raise SystemExit(f"missing {exp}/{variant}/{metric}")
    return s


def _ceilings() -> dict[str, float]:
    """iperf3 mean Mbit/s from kratos (the source in every run), data/baseline/summary.csv."""
    out = {}
    with (ROOT / "data" / "baseline" / "summary.csv").open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["metric"] == "throughput_mbps" and r["src"] == "kratos":
                out[r["dst"]] = float(r["mean"])
    return out


def main() -> int:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update({"font.family": "serif", "font.size": 9})
    an = _analyze()
    main_batch = _stats(an, ROOT / "data")
    night = _stats(an, ROOT / "data" / "ftp")

    # ---- FTP vs meshsync: bytes received after a 1 KiB edit (log scale) --------------------
    cases = ["1 KiB overwrite", "1 KiB insert at 0"]
    ftp = [_get(an, main_batch, "ftp", v, "chunk_bytes").mean for v in ("overwrite", "insert")]
    ms = [
        _get(an, main_batch, "full_delta", "delta", "chunk_bytes").mean,
        _get(an, main_batch, "fixed_cdc", "cdc", "chunk_bytes").mean,
    ]
    fig, ax = plt.subplots(figsize=(3.3, 2.5))
    x = range(len(cases))
    ax.bar([i - 0.2 for i in x], ftp, width=0.4, color=ORANGE, label="FTP")
    ax.bar([i + 0.2 for i in x], ms, width=0.4, color=BLUE, label="meshsync")
    ax.set_yscale("log")
    ax.set_xticks(list(x), cases)
    ax.set_ylabel("bytes received")
    ax.set_ylim(1e4, 1e9)
    ax.legend(frameon=False, loc="upper center", ncol=2)
    fig.tight_layout()
    fig.savefig(OUT / "ftp-bytes.pdf")
    plt.close(fig)

    # ---- throughput (Mbit/s) per condition against the iperf3 ceiling ----------------------
    bars = [
        ("clean", main_batch, "impaired", "clean", BLUE),
        ("5 % loss", main_batch, "impaired", "loss", BLUE),
        ("+50 ms", main_batch, "impaired", "delay", BLUE),
        ("plain", main_batch, "tls", "plain", BLUE),
        ("TLS", main_batch, "tls", "tls", BLUE),
        ("meshsync\n(FTP night)", night, "meshsync_same_night", "clean", BLUE),
        ("FTP", main_batch, "ftp", "full", ORANGE),
    ]
    vals = [_get(an, st, e, v, "throughput_mbps") for _, st, e, v, _ in bars]  # MB/s
    fig, ax = plt.subplots(figsize=(3.3, 2.5))
    ax.bar(
        range(len(bars)),
        [8 * s.mean for s in vals],
        yerr=[8 * (s.sd or 0.0) for s in vals],
        capsize=3,
        color=[c for *_, c in bars],
    )
    ceil = _ceilings()
    ax.set_ylim(0, 1.25 * max(ceil.values()))
    for dst, style in (("defalt", "--"), ("minipc", ":")):
        ax.axhline(ceil[dst], color="black", linestyle=style, linewidth=0.8)
        ax.text(len(bars) - 0.5, ceil[dst], f"iperf3 to {dst}", ha="right", va="bottom", fontsize=7)
    ax.set_xticks(range(len(bars)), [b[0] for b in bars], rotation=45, ha="right", fontsize=7)
    ax.set_ylabel("throughput (Mbit/s)")
    fig.tight_layout()
    fig.savefig(OUT / "throughput-ceiling.pdf")
    plt.close(fig)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
