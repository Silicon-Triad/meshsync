"""Tests for scripts/measure/ftp_baseline.py (#95): no network, no FTP server, no SSH."""

import argparse
import hashlib
import importlib.util
import json
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

_DIR = Path(__file__).resolve().parents[2] / "scripts" / "measure"
_spec = importlib.util.spec_from_file_location("ftp_baseline", _DIR / "ftp_baseline.py")
fb = importlib.util.module_from_spec(_spec)
sys.modules["ftp_baseline"] = fb
_spec.loader.exec_module(fb)
mr, hs = fb.mr, fb.hs


class FakeFTP:
    """Stands in for ftplib.FTP: serves one file in blocks and records the calls."""

    def __init__(self, data: bytes) -> None:
        self.data, self.calls = data, []

    def connect(self, host: str, timeout: float) -> None:
        self.calls.append(("connect", host))

    def login(self, user: str, password: str) -> None:
        self.calls.append(("login", user))

    def retrbinary(self, cmd: str, callback, blocksize: int) -> None:  # noqa: ANN001
        self.calls.append(("retr", cmd))
        for i in range(0, len(self.data), 7):
            callback(self.data[i : i + 7])

    def quit(self) -> None:
        self.calls.append(("quit",))

    def close(self) -> None:
        self.calls.append(("close",))


def test_download_counts_bytes_and_hashes_the_file() -> None:
    data = b"meshsync vs ftp " * 40
    ftp = FakeFTP(data)
    r = fb.download("10.0.0.1", "ftptest", "pw", "large.bin", ftp_factory=lambda: ftp)
    assert r["bytes"] == len(data)
    assert r["sha256"] == hashlib.sha256(data).hexdigest()
    assert 0 <= r["transfer_s"] <= r["e2e_s"]  # RETR starts after connect + login
    assert ("retr", "RETR large.bin") in ftp.calls and ftp.calls[-1] == ("close",)


def test_parse_result_rejects_a_bad_hash_and_missing_output() -> None:
    ok = {"bytes": 3, "sha256": "x", "e2e_s": 1.0, "transfer_s": 0.5, "ok": True}
    assert fb.parse_result("minipc", "noise\n" + json.dumps(ok)) == ok
    with pytest.raises(RuntimeError, match="wrong SHA-256"):
        fb.parse_result("minipc", json.dumps(dict(ok, ok=False)))
    with pytest.raises(RuntimeError, match="no result"):
        fb.parse_result("minipc", "")


def test_row_has_exactly_the_run_py_columns() -> None:
    r = fb.row("full", 2, "minipc", mr.A, 100, "kratos",
               {"bytes": 100, "e2e_s": 3.0, "transfer_s": 2.5})  # fmt: skip
    assert set(r) == set(mr.FIELDS)  # analysis/analyze.py reads it like the meshsync runs
    assert (r["experiment"], r["chunk_bytes"], r["sources"]) == ("ftp", "100", "1")
    assert r["transfer_s"] == mr.fmt(2.5) and r["source_bytes"] == "kratos:100"


def test_place_replaces_the_served_file(tmp_path: Path) -> None:
    src = tmp_path / "a.bin"
    src.write_bytes(b"new")
    root = tmp_path / "root"
    root.mkdir()
    (root / fb.TARGET).write_bytes(b"old")
    fb.place(src, root)
    assert (root / fb.TARGET).read_bytes() == b"new"
    assert sorted(p.name for p in root.iterdir()) == [fb.TARGET]  # no temp file left


def test_client_script_quotes_and_never_contains_the_password() -> None:
    h = hs.Host("minipc", "1", "u@1")
    h.repo = "/home/u/my repo"
    args = argparse.Namespace(user="ftptest", pass_file="~/.meshsync/ftp.pass")
    s = fb.client_script(h, args, "192.168.100.92", "ab" * 32)
    assert "'/home/u/my repo'" in s and " get 192.168.100.92 ftptest " in s
    assert "ftp.pass" in s  # only the file name: the password is read on the client


def test_measure_run_applies_loss_only_to_the_loss_variant(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    testdata = tmp_path / "testdata"
    for rel in (mr.A, mr.OVERWRITE, mr.INSERT):
        (testdata / rel).parent.mkdir(parents=True, exist_ok=True)
        (testdata / rel).write_bytes(rel.encode())
    root = tmp_path / "root"
    root.mkdir()
    profiles: list = []

    @contextmanager
    def fake_netem(iface: str, profile: list[str] | None):  # noqa: ANN202
        profiles.append(profile)
        yield

    def fake_fetch(clients, args, server, sha):  # noqa: ANN001, ANN202
        n = (root / fb.TARGET).stat().st_size
        return {h.name: {"bytes": n, "e2e_s": 2.0, "transfer_s": 1.0} for h in clients}

    monkeypatch.setattr(hs, "netem_link", fake_netem)
    monkeypatch.setattr(fb, "fetch_all", fake_fetch)
    args = argparse.Namespace(testdata=testdata, ftp_root=root, iface="wlp2s0", loss_pct=5.0)
    clients = [hs.Host("minipc", "1", "u@1"), hs.Host("defalt", "2", "u@2")]
    manifest = {rel: "x" for rel in (mr.A, mr.OVERWRITE, mr.INSERT)}
    rows = fb.measure_run(1, hs.Host("kratos", "3"), clients, args, manifest)
    assert profiles == [None, None, None, ["loss", "5"]]
    assert [r["variant"] for r in rows] == ["full"] * 2 + ["overwrite"] * 2 + ["insert"] * 2 + [
        "loss"
    ] * 2
    assert all(r["chunk_bytes"] == str((testdata / r["file"]).stat().st_size) for r in rows)


def test_cli_rejects_bad_hosts(tmp_path: Path) -> None:
    base = ["--ftp-root", str(tmp_path), "--testdata", str(tmp_path), "--data", str(tmp_path)]
    for hosts in (
        ["kratos:1:u@1", "minipc:2:u@2"],  # server must be this machine
        ["kratos:1", "minipc:2"],  # client without SSH target
        ["kratos:1", "kratos:2:u@2"],  # duplicate names
    ):
        with pytest.raises(SystemExit):
            fb.main([*hosts, *base])
    with pytest.raises(SystemExit):  # FTP root must exist
        fb.main(["kratos:1", "minipc:2:u@2", "--ftp-root", str(tmp_path / "nope")])
