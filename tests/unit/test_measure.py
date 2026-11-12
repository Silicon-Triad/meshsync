"""Tests for scripts/measure/run.py helpers (#20). Pure functions only: no peers, no network."""

import importlib.util
import sys
from pathlib import Path

import pytest

from meshsync.metrics import logger as metrics
from meshsync.protocol.constants import META_DIR

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "measure" / "run.py"
_spec = importlib.util.spec_from_file_location("measure_run", _PATH)
mr = importlib.util.module_from_spec(_spec)
sys.modules["measure_run"] = mr  # dataclasses need the module registered
_spec.loader.exec_module(mr)


def M(t: float, d: str, peer: str, ty: str, size: int) -> "mr.Msg":  # noqa: N802
    return mr.Msg(t, d, peer, ty, size)


def test_read_metrics_parses_peer_py_csv(tmp_path: Path) -> None:
    path = tmp_path / "m.csv"
    path.write_text(metrics.CSV_HEADER + "\n1.500000,recv,p1,CHUNK_DATA,100\n2.0,send,p1,PING,20\n")
    assert mr.read_metrics(path) == [
        M(1.5, "recv", "p1", "CHUNK_DATA", 100),
        M(2.0, "send", "p1", "PING", 20),
    ]


def test_window_counts_only_rows_inside() -> None:
    msgs = [
        M(0.5, "recv", "p1", "CHUNK_DATA", 999),  # before t0: an earlier step
        M(1.0, "send", "p1", "CHUNK_REQ", 80),
        M(1.1, "send", "p2", "CHUNK_REQ", 80),
        M(1.4, "recv", "p2", "CHUNK_DATA", 300),
        M(1.6, "recv", "p1", "CHUNK_DATA", 500),
        M(1.7, "recv", "p1", "PONG", 20),
        M(9.0, "recv", "p1", "CHUNK_DATA", 999),  # after t1
    ]
    w = mr.window(msgs, 1.0, 2.0)
    assert w.chunk_msgs == 2 and w.chunk_bytes == 800
    assert w.source_bytes == {"p1": 500, "p2": 300}
    assert w.recv_bytes == 820 and w.sent_bytes == 160
    assert w.transfer_s == pytest.approx(0.6)  # first CHUNK_REQ 1.0 -> last CHUNK_DATA 1.6


def test_window_first_notify_and_no_transfer() -> None:
    msgs = [M(3.0, "send", "p2", "NOTIFY", 50), M(2.5, "send", "p3", "NOTIFY", 50)]
    w = mr.window(msgs, 2.0, 4.0)
    assert w.first_notify == 2.5
    assert w.transfer_s is None and w.chunk_bytes == 0


def test_one_chunk_recorded_out_of_order_is_zero_not_negative() -> None:
    # the send row is written after sendall() returns, so the reply can carry an earlier time
    msgs = [M(1.00010, "send", "p1", "CHUNK_REQ", 80), M(1.00005, "recv", "p1", "CHUNK_DATA", 9)]
    assert mr.window(msgs, 1.0, 2.0).transfer_s == 0.0


def test_read_manifest(tmp_path: Path) -> None:
    (tmp_path / "MANIFEST.sha256").write_text("aa  large/large.bin\nbb  edits/insert.bin\n")
    assert mr.read_manifest(tmp_path) == {"large/large.bin": "aa", "edits/insert.bin": "bb"}


def test_place_is_atomic_and_leaves_no_stage(tmp_path: Path) -> None:
    src = tmp_path / "src.bin"
    src.write_bytes(b"x" * 1000)
    sync = tmp_path / "sync"
    old = sync / "large.bin"
    old.parent.mkdir()
    old.write_bytes(b"old")
    before = old.stat().st_ino
    t = mr.place(src, sync, "large.bin")
    assert (sync / "large.bin").read_bytes() == b"x" * 1000
    assert (sync / "large.bin").stat().st_ino != before  # replaced, so wait_files sees a change
    assert t > 0
    assert list((sync / META_DIR).iterdir()) == []  # staged under .meshsync/, then moved


def test_indexed_reads_index_json(tmp_path: Path) -> None:
    p = mr.Peer("p1", tmp_path, 0)
    assert not mr.indexed(p, "large.bin", "aa")
    (p.data / META_DIR).mkdir(parents=True)
    (p.data / META_DIR / "index.json").write_text(
        '{"rev": 1, "entries": [{"path": "large.bin", "file_hash": "aa"}]}'
    )
    assert mr.indexed(p, "large.bin", "aa")
    assert not mr.indexed(p, "large.bin", "bb")


def test_connected_reads_synced_lines(tmp_path: Path) -> None:
    p, q, r = (mr.Peer(n, tmp_path / n, 0) for n in ("p1", "p2", "p3"))
    p.root.mkdir()
    p.log.write_text("x INFO meshsync.net.connection: connection to p2 is SYNCED\n")
    assert mr.connected(p, [q])
    assert not mr.connected(p, [q, r])


def test_rows_have_every_field(tmp_path: Path) -> None:
    out = tmp_path / "d" / "run1.csv"
    mr.write_rows(out, [dict.fromkeys(mr.FIELDS, "1")])
    assert out.read_text().splitlines()[0] == ",".join(mr.FIELDS)


def test_cli_rejects_missing_testdata(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        mr.main(["full_delta", "--testdata", str(tmp_path), "--data", str(tmp_path)])
    (tmp_path / "MANIFEST.sha256").write_text("aa  large/large.bin\n")  # D missing
    with pytest.raises(SystemExit):
        mr.main(["full_delta", "--testdata", str(tmp_path), "--data", str(tmp_path)])
    assert not (tmp_path / "full_delta").exists()  # nothing was run


def test_suspect_flags_duplicate_downloads() -> None:
    # #59 review / #60: a run that downloaded chunks twice must not look valid
    size = 100 * 1024 * 1024
    assert mr.suspect(104_874_436, size) == ""  # one copy plus 12-byte headers
    assert mr.suspect(209_285_561, size) == "dup"  # the #60 double download


def test_clear_runs_removes_stale_results(tmp_path: Path) -> None:
    for k in (1, 2, 3):
        (tmp_path / f"run{k}.csv").write_text("x")
    (tmp_path / "notes.txt").write_text("keep")
    mr.clear_runs(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["notes.txt"]
    mr.clear_runs(tmp_path / "missing")  # no folder yet: nothing to do


def test_place_removes_stage_when_copy_fails(tmp_path: Path) -> None:
    sync = tmp_path / "sync"
    with pytest.raises(FileNotFoundError):
        mr.place(tmp_path / "does-not-exist", sync, "large.bin")
    assert list((sync / META_DIR).iterdir()) == []
    assert not (sync / "large.bin").exists()


def test_new_columns_are_in_fields() -> None:
    assert {"e2e_from", "poll_s", "suspect"} <= set(mr.FIELDS)


# ---------------------------------------------------------------- conditions 5 and 6 (#69)


def test_setup_s_is_a_column_left_empty_by_other_experiments(tmp_path: Path) -> None:
    assert mr.FIELDS[-1] == "setup_s"
    out = tmp_path / "run1.csv"
    mr.write_rows(out, [{f: "1" for f in mr.FIELDS if f != "setup_s"}])
    assert out.read_text().splitlines()[1].endswith(",1,")  # empty setup_s cell


LOG = (
    "2026-11-12 20:33:22,251 INFO meshsync.__main__: config: ...\n"
    "2026-11-12 20:33:22,260 INFO meshsync.net.manager: peer manager started: p2 on tcp 1 (tls)\n"
    "2026-11-12 20:33:22,445 INFO meshsync.net.auth: responder handshake ok with p3 (AA)\n"
    "2026-11-12 20:33:22,466 INFO meshsync.net.connection: connection to p3 is SYNCED\n"
    "2026-11-12 20:33:23,010 INFO meshsync.net.connection: connection to p1 is SYNCED\n"
)


def test_setup_time_from_started_to_synced_with_the_source() -> None:
    assert mr.setup_time(LOG, "p1") == pytest.approx(0.750)
    assert mr.setup_time(LOG, "p3") == pytest.approx(0.206)
    assert mr.setup_time(LOG, "p9") is None  # never SYNCED
    assert mr.setup_time("connection to p1 is SYNCED\n", "p1") is None  # no start line


def test_started_reads_the_log(tmp_path: Path) -> None:
    p = mr.Peer("p2", tmp_path, 0)
    assert not mr.started(p)  # no log yet
    p.log.write_text(LOG)
    assert mr.started(p)


def test_peer_args_plain_vs_tls(tmp_path: Path) -> None:
    mesh = mr.LocalMesh(tmp_path, 2, "cdc", 47000, 47000)
    p1, p2 = mesh.peers
    args = mesh.peer_args(p1)
    assert "--plain" in args and "--tls" not in args
    assert args[args.index("--peer") + 1] == f"127.0.0.1:{p2.port}"
    mesh.tls = True  # what tls=True sets, without running gen_certs.sh here
    args = mesh.peer_args(p1)
    assert "--plain" not in args
    assert args[args.index("--tls") + 1 : args.index("--tls") + 3] == [
        "--cert-dir",
        str(mesh.certs),
    ]


def ns(**kw: object) -> "mr.argparse.Namespace":
    return mr.argparse.Namespace(**kw)


def test_impaired_profiles_use_the_defaults() -> None:
    profiles = mr.impaired_profiles(ns(loss_pct=mr.LOSS_PCT_DEFAULT, delay_ms=mr.DELAY_MS_DEFAULT))
    assert profiles == [("clean", None), ("loss", ["loss", "5"]), ("delay", ["delay", "50"])]
    assert mr.impaired_profiles(ns(loss_pct=2.5, delay_ms=20))[1:] == [
        ("loss", ["loss", "2.5"]),
        ("delay", ["delay", "20"]),
    ]


def test_netem_cmd_uses_sudo_only_when_not_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mr.os, "geteuid", lambda: 0)
    assert mr.netem_cmd("clear", "lo") == [str(mr.NETEM_SH), "clear", "lo"]
    monkeypatch.setattr(mr.os, "geteuid", lambda: 1000)
    assert mr.netem_cmd("clear", "lo") == ["sudo", "-n", str(mr.NETEM_SH), "clear", "lo"]


def _record(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []
    monkeypatch.setattr(mr.os, "geteuid", lambda: 0)
    monkeypatch.setattr(mr.subprocess, "run", lambda cmd, **kw: calls.append(cmd[1:]))
    return calls


def test_netem_applies_then_clears(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _record(monkeypatch)
    with mr.netem("lo", ["loss", "5"]):
        assert calls == [["clear", "lo"], ["apply", "lo", "loss", "5"]]
    assert calls[-1] == ["clear", "lo"]


def test_netem_clears_even_when_the_run_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _record(monkeypatch)
    with pytest.raises(TimeoutError), mr.netem("lo", ["delay", "50"]):
        raise TimeoutError("receiver never got the file")
    assert calls[-1] == ["clear", "lo"]  # the link is never left impaired


def test_netem_clean_profile_applies_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _record(monkeypatch)
    with mr.netem("lo", None):
        pass
    assert calls == [["clear", "lo"], ["clear", "lo"]]


def test_netem_profiles_match_netem_sh(monkeypatch: pytest.MonkeyPatch) -> None:
    # The arguments the runner builds must be accepted by netem.sh (dry run: no root, no tc).
    monkeypatch.setenv("NETEM_DRY_RUN", "1")
    for _, profile in mr.impaired_profiles(ns(loss_pct=5.0, delay_ms=50.0))[1:]:
        out = mr.subprocess.run(
            [str(mr.NETEM_SH), "apply", "lo", *profile], capture_output=True, text=True, check=True
        )
        assert out.stdout.startswith("tc qdisc replace dev lo root netem")
