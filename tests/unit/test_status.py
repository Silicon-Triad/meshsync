"""Tests for status.py and the `meshsync status` / `stats` commands (#64, §14)."""

import json
import os
import threading
import time
from pathlib import Path

import pytest

from meshsync import status as status_mod
from meshsync.__main__ import main
from meshsync.metrics.logger import CSV_HEADER
from meshsync.status import (
    STATUS_STALE_AFTER,
    StatusWriter,
    status_path,
    status_report,
    summarise_csv,
)

PEER = {"name": "bob", "peer_id": "B77C0E11", "addr": "10.0.0.2", "tcp_port": 47001,
        "state": "SYNCED", "last_seen": None}  # fmt: skip


def write_status(sync_dir: Path, *, age: float = 0.0, stopped: bool = False) -> None:
    path = status_path(sync_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    data = {"name": "alice", "peer_id": "A1F3C9D2", "mode": "tls", "tcp_port": 47001,
            "udp_port": 47000, "pid": 1, "started_at": now - age - 5, "updated_at": now - age,
            "index_rev": 7, "entries": 3, "tombstones": 1, "peers": [PEER],
            "stopped": stopped}  # fmt: skip
    path.write_text(json.dumps(data))


# ---------------------------------------------------------------- writer


def test_writer_is_atomic_and_marks_stopped(tmp_path: Path) -> None:
    w = StatusWriter(tmp_path, {"name": "alice"})
    w.start(lambda: {"index_rev": 1, "entries": 0, "tombstones": 0, "peers": [PEER]})
    data = json.loads(status_path(tmp_path).read_text())
    assert data["name"] == "alice" and data["peers"] == [PEER] and data["stopped"] is False
    assert {"pid", "started_at", "updated_at"} <= set(data)
    w.stop()
    data = json.loads(status_path(tmp_path).read_text())
    assert data["stopped"] is True and data["peers"] == [] and data["index_rev"] == 1
    assert [p.name for p in status_path(tmp_path).parent.iterdir()] == ["status.json"]
    w.update()  # after stop: ignored, file stays stopped
    assert json.loads(status_path(tmp_path).read_text())["stopped"] is True


def test_writer_snapshot_failure_is_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def broken() -> dict:
        raise RuntimeError("index gone")

    w = StatusWriter(tmp_path, {})
    w.start(broken)  # must not raise
    w.stop()
    assert "could not write" in caplog.text


# ---------------------------------------------------------------- status


def test_status_running_shows_peers(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    write_status(tmp_path)
    assert main(["status", "--dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("running")
    assert "bob" in out and "B77C0E11" in out and "10.0.0.2:47001" in out and "SYNCED" in out
    assert "rev 7, 3 files, 1 tombstones" in out


def test_status_stale_file_is_not_running(tmp_path: Path) -> None:
    write_status(tmp_path, age=STATUS_STALE_AFTER + 1)
    code, text = status_report(tmp_path)
    assert code == 1
    assert text.startswith("not running (last update")
    assert "bob" in text  # last known state still shown
    write_status(tmp_path, age=STATUS_STALE_AFTER - 1)
    assert status_report(tmp_path)[0] == 0


def test_status_stopped_file_is_not_running(tmp_path: Path) -> None:
    write_status(tmp_path, stopped=True)
    code, text = status_report(tmp_path)
    assert code == 1 and text.startswith("not running (stopped")


def test_status_missing_or_bad_file(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    assert main(["status", "--dir", str(tmp_path)]) == 1
    assert "not running (no status file)" in capsys.readouterr().out
    status_path(tmp_path).parent.mkdir()
    status_path(tmp_path).write_text("{not json")
    assert status_report(tmp_path)[0] == 1


def test_status_dir_from_config_and_flag_overrides(tmp_path: Path) -> None:
    from_cfg, from_flag = tmp_path / "cfgdir", tmp_path / "flagdir"
    write_status(from_flag)
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'sync_dir = "{from_cfg}"\n')
    assert main(["status", "--config", str(cfg)]) == 1  # config dir has no status file
    assert main(["status", "--config", str(cfg), "--dir", str(from_flag)]) == 0


# ---------------------------------------------------------------- stats

ROWS = [
    "100.000000,send,bob,PING,20",
    "100.500000,recv,bob,PONG,20",
    "101.000000,recv,bob,CHUNK_DATA,1000",
    "102.000000,recv,carol,CHUNK_DATA,500",
    "103.000000,send,carol,CHUNK_REQ,90",
]


def write_csv(path: Path, rows: list[str]) -> Path:
    path.write_text("\n".join([CSV_HEADER, *rows]) + "\n")
    return path


def test_stats_totals(tmp_path: Path) -> None:
    s = summarise_csv(write_csv(tmp_path / "m.csv", ROWS))
    assert (s.total.count, s.total.bytes) == (5, 1630)
    assert (s.by_type[("CHUNK_DATA", "recv")].count, s.by_type[("CHUNK_DATA", "recv")].bytes) == (
        2,
        1500,
    )
    assert (s.by_direction["send"].count, s.by_direction["send"].bytes) == (2, 110)
    assert (s.by_direction["recv"].count, s.by_direction["recv"].bytes) == (3, 1520)
    assert (s.by_peer["bob"].bytes, s.by_peer["carol"].bytes) == (1040, 590)
    assert (s.first_t, s.last_t, s.skipped) == (100.0, 103.0, 0)


def test_stats_skips_malformed_rows(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    bad = ["x,send,bob,PING,20", "104.0,sideways,bob,PING,20", "105.0,send,bob,PING", ""]
    path = write_csv(tmp_path / "m.csv", ROWS + bad)
    s = summarise_csv(path)
    assert s.total.count == 5 and s.skipped == 3  # the blank line is not a row
    assert main(["stats", "--metrics-csv", str(path)]) == 0
    out = capsys.readouterr().out
    assert "total    5 messages, 1630 bytes" in out and "skipped 3 malformed rows" in out


def test_stats_path_from_config(tmp_path: Path) -> None:
    path = write_csv(tmp_path / "m.csv", ROWS)
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'metrics_csv = "{path}"\n')
    assert main(["stats", "--config", str(cfg)]) == 0


def test_stats_unset_or_missing(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text("")
    assert main(["stats", "--config", str(cfg)]) == 1
    assert "no metrics CSV" in capsys.readouterr().err
    assert main(["stats", "--metrics-csv", str(tmp_path / "nope.csv")]) == 1
    assert "not found" in capsys.readouterr().err
    (tmp_path / "wrong.csv").write_text("a,b\n")
    assert main(["stats", "--metrics-csv", str(tmp_path / "wrong.csv")]) == 1


def test_update_in_progress_cannot_overwrite_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # PR #86 review: an update() that passed the stopped check must not os.replace its
    # "stopped": false file after stop() has written "stopped": true.
    monkeypatch.setattr(status_mod, "SCAN_INTERVAL", 60)  # no periodic refresh in this test
    w = StatusWriter(tmp_path, {})
    w.start(lambda: {"peers": []})
    real_replace = os.replace
    in_replace, release = threading.Event(), threading.Event()

    def slow_replace(src: str, dst: object) -> None:
        if threading.current_thread().name == "updater":
            in_replace.set()
            release.wait(5)
        real_replace(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr(status_mod.os, "replace", slow_replace)
    updater = threading.Thread(target=w.update, name="updater")
    updater.start()
    assert in_replace.wait(5)  # the update is past the stopped check
    stopper = threading.Thread(target=w.stop, name="stopper")
    stopper.start()
    stopper.join(1.0)  # without the lock stop() finishes here; with it, it waits
    release.set()
    updater.join(5)
    stopper.join(5)
    assert json.loads(status_path(tmp_path).read_text())["stopped"] is True
