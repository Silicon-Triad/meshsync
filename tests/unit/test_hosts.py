"""Tests for scripts/measure/hosts.py (#77): pure helpers, no network, no SSH."""

import argparse
import importlib.util
import sys
from pathlib import Path

import pytest

from meshsync.protocol.constants import TCP_PORT

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "measure" / "hosts.py"
_spec = importlib.util.spec_from_file_location("measure_hosts", _PATH)
hs = importlib.util.module_from_spec(_spec)
sys.modules["measure_hosts"] = hs  # dataclasses need the module registered
_spec.loader.exec_module(hs)
mr = hs.mr


def M(t: float, d: str, peer: str, ty: str, size: int) -> "mr.Msg":  # noqa: N802
    return mr.Msg(t, d, peer, ty, size)


def test_parse_host_forms() -> None:
    h = hs.parse_host("kratos:192.168.100.92")
    assert (h.name, h.ip, h.target, h.port, h.local) == ("kratos", "192.168.100.92", None,
                                                         TCP_PORT, True)  # fmt: skip
    h = hs.parse_host("minipc:192.168.100.190:user@192.168.100.190")
    assert (h.target, h.local) == ("user@192.168.100.190", False)
    h = hs.parse_host("b:127.0.0.1::47013")  # empty SSH target = local, custom port
    assert (h.target, h.port) == (None, 47013)
    for bad in ("kratos", ":1.2.3.4", "a:1:2:3:4", "a:"):
        with pytest.raises(ValueError):
            hs.parse_host(bad)


def test_shell_argv_local_vs_remote() -> None:
    assert hs.shell_argv(hs.Host("a", "1"), "echo hi") == ["bash", "-c", "echo hi"]
    argv = hs.shell_argv(hs.Host("b", "2", "user@host"), "echo 'x y'")
    assert argv[0] == "ssh" and "ControlMaster=auto" in argv and argv[-2] == "user@host"
    assert argv[-1] == "bash -c 'echo '\"'\"'x y'\"'\"''"  # the whole script is one quoted arg


def test_parse_synced_and_stat() -> None:
    log = "x connection to b is SYNCED\ny other\nz connection to c is SYNCED\n"
    assert hs.parse_synced(log) == {"b", "c"}
    assert hs.parse_stat("1234 104857600 1794751149.5\n") == (1234, 104857600, 1794751149.5)
    assert hs.parse_stat("- - 1794751149.5") is None  # file not there yet
    assert hs.parse_stat("") is None


def test_row_e2e_uses_only_the_receivers_clock() -> None:
    # Receiver clock ~1000 s; the source's clock is unrelated (another host, another zone).
    rmsgs = [
        M(1000.0, "recv", "src", "PING", 30),  # before the step: outside the window
        M(1010.0, "recv", "src", "NOTIFY", 900),  # first NOTIFY received -> e2e start
        M(1010.1, "send", "src", "CHUNK_REQ", 88),
        M(1012.5, "recv", "src", "CHUNK_DATA", 600),
        M(1013.0, "recv", "src", "CHUNK_DATA", 400),  # last chunk -> e2e end
    ]
    step = mr.Step("full_delta", "full", 1, mr.A)
    r = hs.row(step, "rcv", rmsgs, r_t0=1005.0, r_t1=1014.0, size=1000, notify_s=7.5, poll=0.5)
    assert r["e2e_s"] == mr.fmt(3.0) and r["e2e_from"] == "first_notify"
    assert r["notify_s"] == mr.fmt(7.5)  # the source-clock number is passed in, not mixed
    assert r["transfer_s"] == mr.fmt(2.9)
    assert (r["chunk_bytes"], r["sources"], r["suspect"]) == ("1000", "1", "")
    assert set(r) == set(mr.FIELDS) - {"setup_s"}


def test_row_sources_counts_from_joiner_start() -> None:
    rmsgs = [M(50.2, "send", "h1", "CHUNK_REQ", 88), M(52.0, "recv", "h1", "CHUNK_DATA", 10)]
    step = mr.Step("sources", "1src", 1, mr.A)
    r = hs.row(step, "joiner", rmsgs, 50.0, 53.0, 10, None, 0.5, start=50.0)
    assert (r["e2e_s"], r["e2e_from"], r["notify_s"]) == (mr.fmt(2.0), "joiner_start", "")


def test_row_without_notify_has_no_e2e() -> None:
    rmsgs = [M(5.0, "recv", "s", "CHUNK_DATA", 10)]
    r = hs.row(mr.Step("x", "v", 1, mr.A), "r", rmsgs, 0.0, 9.0, 10, None, 0.5)
    assert r["e2e_s"] == ""  # missing NOTIFY: no guessed number


def test_impaired_refuses_a_remote_source() -> None:
    hosts = [hs.Host("minipc", "1", "user@1"), hs.Host("kratos", "2")]
    with pytest.raises(ValueError, match="must be this machine"):
        hs.exp_impaired(1, hosts, argparse.Namespace(), {}, {})


def test_cli_rejects_bad_hosts(tmp_path: Path) -> None:
    for hosts in (["a:1"], ["a:1", "a:2"], ["a:1", "bad"]):
        with pytest.raises(SystemExit):
            hs.main(["full_delta", *hosts, "--testdata", str(tmp_path), "--data", str(tmp_path)])


def test_fetch_local_same_path_is_a_noop(tmp_path: Path) -> None:
    # Real 3-host run (#91): with the default --data, the local host's peer writes its
    # metrics.csv exactly where the driver collects it; copying it onto itself crashed.
    f = tmp_path / "raw" / "kratos" / "metrics.csv"
    f.parent.mkdir(parents=True)
    f.write_text("t,direction,peer,type,size\n")
    hs.fetch(hs.Host("kratos", "1"), str(f), f)
    assert f.read_text() == "t,direction,peer,type,size\n"
    other = tmp_path / "copy" / "metrics.csv"
    hs.fetch(hs.Host("kratos", "1"), str(f), other)  # different path: still copied
    assert other.read_text() == f.read_text()


def _runs(monkeypatch: pytest.MonkeyPatch, fail_first_clear: bool) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake(cmd: list[str], **kw: object) -> "hs.subprocess.CompletedProcess[bytes]":
        calls.append(list(cmd))
        if fail_first_clear and len(calls) == 1:
            raise hs.subprocess.CalledProcessError(1, cmd)
        return hs.subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(hs.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(mr.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(hs.subprocess, "run", fake)
    return calls


def test_clear_netem_falls_back_to_interactive_sudo(monkeypatch: pytest.MonkeyPatch) -> None:
    # Real 3-host run (#91): sudo's cache expired mid-batch and `sudo -n ... clear` failed,
    # leaving 5 % loss on the Wi-Fi. The clear must fall back to asking for the password.
    calls = _runs(monkeypatch, fail_first_clear=True)
    hs.clear_netem("wlp2s0")
    assert calls[0][:2] == ["sudo", "-n"]  # non-interactive first
    assert calls[1] == ["sudo", str(mr.NETEM_SH), "clear", "wlp2s0"]  # then interactive


def test_netem_link_clears_even_when_the_run_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _runs(monkeypatch, fail_first_clear=False)
    with pytest.raises(TimeoutError), hs.netem_link("wlp2s0", ["loss", "5"]):
        raise TimeoutError("receiver never got the file")
    assert calls[-1][-2:] == ["clear", "wlp2s0"]
    assert any(c[-3:] == ["wlp2s0", "loss", "5"] for c in calls)


def test_keep_sudo_alive_refreshes_until_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _runs(monkeypatch, fail_first_clear=False)
    stop = hs.threading.Event()
    t = hs.threading.Thread(target=hs.keep_sudo_alive, args=(stop, 0.01))
    t.start()
    hs.time.sleep(0.1)
    stop.set()
    t.join(1)
    assert not t.is_alive() and calls and all(c == ["sudo", "-n", "-v"] for c in calls)
