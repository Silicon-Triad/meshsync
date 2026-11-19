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


def _fake_ssh(monkeypatch: pytest.MonkeyPatch, codes: list[int]) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake(cmd: list[str], **kw: object) -> "hs.subprocess.CompletedProcess[str]":
        calls.append(cmd)
        code = codes[min(len(calls) - 1, len(codes) - 1)]
        return hs.subprocess.CompletedProcess(cmd, code, "out", "Broken pipe")

    monkeypatch.setattr(hs.subprocess, "run", fake)
    monkeypatch.setattr(hs, "SSH_RETRY_WAIT_S", 0)
    return calls


def test_read_only_command_retries_a_dropped_ssh_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Real batch (#77): the shared SSH connection to minipc dropped mid-run
    # ("mux_client_request_session: read from master failed: Broken pipe") and the poll failed.
    calls = _fake_ssh(monkeypatch, [255, 255, 0])
    assert hs.sh(hs.Host("minipc", "1", "u@1"), "stat x", retry=True) == "out"
    assert len(calls) == 3


def test_side_effect_commands_are_never_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_ssh(monkeypatch, [255, 0])
    with pytest.raises(RuntimeError, match="Broken pipe"):
        hs.sh(hs.Host("minipc", "1", "u@1"), "start a peer")  # retry defaults to False
    assert len(calls) == 1


def test_retry_gives_up_after_ssh_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_ssh(monkeypatch, [255])
    with pytest.raises(RuntimeError):
        hs.sh(hs.Host("minipc", "1", "u@1"), "stat x", retry=True)
    assert len(calls) == hs.SSH_RETRIES + 1


def test_ssh_options_keep_the_connection_alive() -> None:
    assert "ServerAliveInterval=15" in hs.SSH_OPTS


def test_resume_runs_only_the_missing_ones(tmp_path: Path) -> None:
    for k in (1, 2, 3, 5):
        (tmp_path / f"run{k}.csv").write_text("x")
    assert hs.runs_to_do(tmp_path, 5, resume=True) == [4]
    assert hs.runs_to_do(tmp_path, 5, resume=False) == [1, 2, 3, 4, 5]
    assert hs.runs_to_do(tmp_path / "none", 2, resume=True) == [1, 2]


def test_stop_retries_the_kill_and_the_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Review on #93: if SSH drops during stop(), the remote peer was never killed and kept
    # holding TCP 47001, so every later run on that host failed. Killing again is harmless.
    seen: list[tuple[str, bool]] = []
    monkeypatch.setattr(hs, "sh", lambda h, s, check=True, retry=False: seen.append((s, retry)))
    monkeypatch.setattr(hs, "fetch", lambda *a: None)
    args = argparse.Namespace(data=tmp_path, keep_data=False)
    hs.HostMesh([hs.Host("minipc", "1", "u@1")], args, "x", 1, "v").stop()
    assert any("kill -INT" in s for s, _ in seen) and all(r for _, r in seen)


def test_fetch_retries_a_dropped_scp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = _fake_ssh(monkeypatch, [1, 0])
    hs.fetch(hs.Host("minipc", "1", "u@1"), "/r/metrics.csv", tmp_path / "m.csv")
    assert len(calls) == 2 and calls[0][0] == "scp"


def test_fetch_gives_up_after_ssh_retries(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = _fake_ssh(monkeypatch, [1])
    with pytest.raises(hs.subprocess.CalledProcessError):
        hs.fetch(hs.Host("minipc", "1", "u@1"), "/r/metrics.csv", tmp_path / "m.csv")
    assert len(calls) == hs.SSH_RETRIES + 1


def test_log_run_records_when_each_run_was_measured(tmp_path: Path) -> None:
    hs.log_run(tmp_path, 4, 0.0, 60.0)
    hs.log_run(tmp_path, 5, 86400.0, 86460.0)  # e.g. redone by --resume the next day
    lines = (tmp_path / hs.RUNS_LOG).read_text().splitlines()
    assert [ln.split()[0] for ln in lines] == ["run4", "run5"]
    assert lines[0].split()[1] != lines[1].split()[1]  # different dates are visible
