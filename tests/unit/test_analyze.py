"""Tests for analysis/analyze.py (#71): small CSVs in tmp_path, no network."""

import csv
import importlib.util
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "analysis" / "analyze.py"
_spec = importlib.util.spec_from_file_location("analyze", _PATH)
an = importlib.util.module_from_spec(_spec)
sys.modules["analyze"] = an  # dataclasses need the module registered
_spec.loader.exec_module(an)

COLS = ["experiment", "variant", "run", "receiver", "transfer_s", "e2e_s", "chunk_bytes",
        "recv_bytes", "suspect", "setup_s"]  # fmt: skip


def write_run(data: Path, exp: str, run: int, rows: list[dict]) -> None:
    path = data / exp / f"run{run}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS, restval="")
        w.writeheader()
        for r in rows:
            w.writerow({"experiment": exp, "run": str(run), **r})


def R(variant: str, rcv: str, transfer: float, nbytes: int, **kw: str) -> dict:  # noqa: N802
    return {"variant": variant, "receiver": rcv, "transfer_s": str(transfer), "e2e_s": "10",
            "chunk_bytes": str(nbytes), **kw}  # fmt: skip


def test_receivers_are_averaged_before_runs(tmp_path: Path) -> None:
    write_run(tmp_path, "tls", 1, [R("plain", "p2", 1.0, 100), R("plain", "p3", 3.0, 100)])
    write_run(tmp_path, "tls", 2, [R("plain", "p2", 4.0, 100), R("plain", "p3", 4.0, 100)])
    rows, _ = an.load(tmp_path)
    s = an.lookup(an.summarize("tls", rows["tls"]), "plain", "transfer_s")
    assert (s.n, s.mean) == (2, 3.0)  # run means 2.0 and 4.0, not 4 separate rows
    assert s.sd == pytest.approx(2**0.5)  # sample sd of [2, 4]


def test_single_run_has_no_sd(tmp_path: Path) -> None:
    write_run(tmp_path, "x", 1, [R("a", "p2", 1.0, 1)])
    rows, _ = an.load(tmp_path)
    assert an.summarize("x", rows["x"])[0].sd is None


def test_dup_rows_are_dropped_and_counted(tmp_path: Path) -> None:
    write_run(tmp_path, "sources", 1, [R("2src", "p3", 1.0, 200, suspect="dup"),
                                       R("1src", "p2", 2.0, 100)])  # fmt: skip
    rows, dropped = an.load(tmp_path)
    assert dropped == {"sources": 1}
    assert {r["variant"] for r in rows["sources"]} == {"1src"}


def test_empty_setup_s_is_skipped_and_filled_one_is_kept(tmp_path: Path) -> None:
    write_run(tmp_path, "tls", 1, [R("plain", "p2", 1.0, 1, setup_s="0.008"),
                                   R("tls", "p2", 1.0, 1, setup_s="0.052")])  # fmt: skip
    write_run(tmp_path, "impaired", 1, [R("clean", "p2", 1.0, 1)])
    rows, _ = an.load(tmp_path)
    assert an.lookup(an.summarize("tls", rows["tls"]), "tls", "setup_s").mean == 0.052
    assert an.lookup(an.summarize("impaired", rows["impaired"]), "clean", "setup_s") is None


def test_throughput_and_zero_transfer() -> None:
    m = an.row_metrics({"transfer_s": "2.0", "chunk_bytes": "10000000"})
    assert m["throughput_mbps"] == 5.0
    assert (
        an.row_metrics({"transfer_s": "0.000000", "chunk_bytes": "75667"})["throughput_mbps"]
        is None
    )


def test_savings_percentages_chunk_only_and_total() -> None:
    S = an.Stat  # noqa: N806
    stats = {
        "full_delta": [S("full_delta", "full", "chunk_bytes", 5, 1000.0, 0.0),
                       S("full_delta", "delta", "chunk_bytes", 5, 10.0, 0.0),
                       S("full_delta", "full", "recv_bytes", 5, 1100.0, 0.0),
                       S("full_delta", "delta", "recv_bytes", 5, 110.0, 0.0)],
        "fixed_cdc": [S("fixed_cdc", "fixed", "chunk_bytes", 5, 200.0, 0.0),
                      S("fixed_cdc", "cdc", "chunk_bytes", 5, 50.0, 0.0)],
    }  # fmt: skip
    got = {(label.split()[0], counted): pct for label, counted, _, _, pct in an.savings(stats)}
    assert got == {
        ("delta", "chunk data only"): pytest.approx(99.0),
        ("delta", "all bytes received"): pytest.approx(90.0),  # metadata counted too (#80)
        ("CDC", "chunk data only"): pytest.approx(75.0),  # no recv_bytes: no total row claimed
    }
    assert an.savings({}) == []  # experiments not run yet: nothing claimed


def test_recv_bytes_is_summarized(tmp_path: Path) -> None:
    write_run(tmp_path, "full_delta", 1, [R("delta", "p2", 0.0, 75667, recv_bytes="170000")])
    rows, _ = an.load(tmp_path)
    assert (
        an.lookup(an.summarize("full_delta", rows["full_delta"]), "delta", "recv_bytes").mean
        == 170000
    )


def test_tables_written(tmp_path: Path) -> None:
    write_run(tmp_path / "data", "sources", 1, [R("1src", "p2", 2.0, 100, suspect="dup")])
    write_run(tmp_path / "data", "sources", 2, [R("1src", "p2", 2.0, 100)])
    out = tmp_path / "out"
    assert an.main(["--data", str(tmp_path / "data"), "--out", str(out), "--no-plots"]) == 0
    md = (out / "sources.md").read_text()
    assert "| 1src | transfer time (s) | 1 | 2 ± — |" in md
    assert "1 row(s) flagged `dup`" in md
    assert (out / "sources.csv").read_text().startswith("experiment,variant,metric,n,mean,sd")


def test_no_data_is_an_error(tmp_path: Path) -> None:
    assert an.main(["--data", str(tmp_path), "--out", str(tmp_path / "o"), "--no-plots"]) == 1


def test_plot_writes_pdf_and_png(tmp_path: Path) -> None:
    pytest.importorskip("matplotlib")
    S = an.Stat  # noqa: N806
    stats = [
        S("tls", "plain", "setup_s", 5, 0.008, 0.001),
        S("tls", "tls", "setup_s", 5, 0.05, 0.002),
        S("tls", "plain", "throughput_mbps", 5, 60.0, 2.0),
    ]
    paths = an.plot(tmp_path, "tls", stats)
    assert [p.suffix for p in paths] == [".pdf", ".png"] and all(p.stat().st_size for p in paths)
