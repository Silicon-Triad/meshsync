"""Tests for scripts/measure/baseline.py (#70): sample ping/iperf3 output, no network."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("baseline", _ROOT / "scripts/measure/baseline.py")
bl = importlib.util.module_from_spec(_spec)
sys.modules["baseline"] = bl  # dataclasses need the module registered
_spec.loader.exec_module(bl)

# Shape of iputils ping output (values from the #67 step-1 check, kratos -> defalt).
PING = """PING 192.168.100.80 (192.168.100.80) 56(84) bytes of data.
64 bytes from 192.168.100.80: icmp_seq=1 ttl=64 time=9.16 ms
64 bytes from 192.168.100.80: icmp_seq=2 ttl=64 time=4.69 ms
64 bytes from 192.168.100.80: icmp_seq=3 ttl=64 time=2.98 ms

--- 192.168.100.80 ping statistics ---
3 packets transmitted, 3 received, 0% packet loss, time 2003ms
rtt min/avg/max/mdev = 2.978/5.608/9.156/2.604 ms
"""
PING_ALL_LOST = """--- 192.168.100.80 ping statistics ---
3 packets transmitted, 0 received, 100% packet loss, time 2030ms
"""


def iperf(bps: float, retr: int = 3) -> str:
    return json.dumps(
        {"end": {"sum_received": {"bits_per_second": bps}, "sum_sent": {"retransmits": retr}}}
    )


def test_parse_ping() -> None:
    assert bl.parse_ping(PING) == (2.978, 5.608, 9.156, 2.604, 0.0)
    assert bl.parse_ping(PING_ALL_LOST) == (None, None, None, None, 100.0)
    with pytest.raises(ValueError):
        bl.parse_ping("ping: connect: Network is unreachable\n")


def test_parse_iperf3() -> None:
    assert bl.parse_iperf3(iperf(50e6, 7)) == (50.0, 7)
    with pytest.raises(ValueError):
        bl.parse_iperf3(json.dumps({"error": "unable to connect to server"}))


def test_load_and_summary(tmp_path: Path) -> None:
    (tmp_path / "ping_a_b_run1.txt").write_text(PING)
    (tmp_path / "ping_a_b_run2.txt").write_text(PING.replace("5.608", "7.608"))
    (tmp_path / "ping_b_a_run1.txt").write_text(PING_ALL_LOST)  # no RTT: not in the summary
    (tmp_path / "iperf3_a_b_run1.json").write_text(iperf(40e6))
    (tmp_path / "iperf3_a_b_run2.json").write_text(iperf(60e6))
    (tmp_path / "iperf3_b_a_run1.json").write_text("{not json")  # skipped with a warning
    (tmp_path / "notes.txt").write_text("ignored")
    pings, iperfs = bl.load(tmp_path)
    assert len(pings) == 3 and len(iperfs) == 2
    s = {(r["metric"], r["src"], r["dst"]): r for r in bl.summary(pings, iperfs)}
    assert set(s) == {("rtt_ms", "a", "b"), ("throughput_mbps", "a", "b")}
    assert (s[("rtt_ms", "a", "b")]["mean"], s[("rtt_ms", "a", "b")]["n"]) == ("6.608", "2")
    assert s[("throughput_mbps", "a", "b")]["mean"] == "50.000"
    assert float(s[("throughput_mbps", "a", "b")]["sd"]) == pytest.approx(14.142, abs=1e-3)


def test_peer_names_with_dashes_and_underscores(tmp_path: Path) -> None:
    (tmp_path / "ping_laptop-a_mini_pc_run3.txt").write_text(PING)
    pings, _ = bl.load(tmp_path)
    # non-greedy name match: the first "_" splits src from dst; names keep their dashes
    assert (pings[0].src, pings[0].run) == ("laptop-a", 3)


def test_pingpong_pairs_each_ping_with_the_next_pong() -> None:
    rows = [
        {"t": "10.000", "direction": "send", "peer": "AA", "type": "PING"},
        {"t": "10.004", "direction": "recv", "peer": "AA", "type": "PONG"},
        {"t": "10.100", "direction": "recv", "peer": "BB", "type": "PONG"},  # no PING: ignored
        {"t": "20.000", "direction": "send", "peer": "BB", "type": "PING"},
        {"t": "20.010", "direction": "recv", "peer": "AA", "type": "PONG"},  # AA not pending
        {"t": "20.012", "direction": "recv", "peer": "BB", "type": "PONG"},
        {"t": "20.500", "direction": "recv", "peer": "AA", "type": "CHUNK_DATA"},
    ]
    got = [(p, round(ms, 3)) for p, ms in bl.pingpong(rows)]
    assert got == [("AA", 4.0), ("BB", 12.0)]


def test_cli_writes_csvs(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "ping_a_b_run1.txt").write_text(PING)
    (raw / "iperf3_a_b_run1.json").write_text(iperf(40e6))
    metrics = tmp_path / "m.csv"
    metrics.write_text("t,direction,peer,type,size\n1.0,send,AA,PING,20\n1.005,recv,AA,PONG,20\n")
    out = tmp_path / "out"
    assert bl.main([str(raw), "--out", str(out), "--metrics", f"a={metrics}"]) == 0
    assert (out / "rtt.csv").read_text().splitlines()[1].startswith("a,b,1,2.978,5.608")
    assert (out / "iperf3.csv").read_text().splitlines()[1] == "a,b,1,40.000,3"
    assert (out / "pingpong.csv").read_text().splitlines()[1] == "a,AA,5.000"
    assert bl.main([str(tmp_path / "out"), "--out", str(out)]) == 1  # no raw files


def test_baseline_sh_usage() -> None:
    sh = _ROOT / "scripts/measure/baseline.sh"
    r = subprocess.run(["bash", str(sh), "only-outdir"], capture_output=True, text=True)
    assert r.returncode == 2 and "usage:" in r.stderr
