"""Tests for scripts/netem.sh in dry-run mode (no tc, no root). Issue #18."""

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "netem.sh"


def run(*args: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "NETEM_DRY_RUN": "1", **(env_extra or {})}
    return subprocess.run(
        ["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env, check=False
    )


def test_loss_defaults_to_five_percent() -> None:
    # §10: "5 % loss via netem"
    r = run("apply", "lo", "loss")
    assert r.returncode == 0
    assert r.stdout.strip() == "tc qdisc replace dev lo root netem loss 5%"


def test_loss_custom_percent() -> None:
    r = run("apply", "eth0", "loss", "1.5")
    assert r.stdout.strip() == "tc qdisc replace dev eth0 root netem loss 1.5%"


def test_delay_requires_value() -> None:
    assert run("apply", "lo", "delay").returncode == 2


def test_delay_applies() -> None:
    r = run("apply", "lo", "delay", "40")
    assert r.returncode == 0
    assert r.stdout.strip() == "tc qdisc replace dev lo root netem delay 40ms"


def fake_tc(tmp_path: Path, show_out: str, rc: int = 0) -> dict[str, str]:
    """Put a fake `tc` on PATH that prints show_out and exits rc."""
    tc = tmp_path / "tc"
    tc.write_text(f"#!/usr/bin/env bash\nprintf '%s\\n' '{show_out}'\nexit {rc}\n")
    tc.chmod(0o755)
    return {"PATH": f"{tmp_path}:{os.environ['PATH']}"}


def test_clear_deletes_netem(tmp_path: Path) -> None:
    env = fake_tc(tmp_path, "qdisc netem 8001: root refcnt 2 limit 1000 loss 5%")
    r = run("clear", "lo", env_extra=env)
    assert r.returncode == 0
    assert r.stdout.strip() == "tc qdisc del dev lo root"


def test_clear_noop_without_netem(tmp_path: Path) -> None:
    env = fake_tc(tmp_path, "qdisc noqueue 0: root refcnt 2")
    r = run("clear", "lo", env_extra=env)
    assert r.returncode == 0
    assert r.stdout.strip() == ""


def test_clear_fails_when_tc_fails(tmp_path: Path) -> None:
    # e.g. a typo'd interface: tc errors, so clear must not report success.
    env = fake_tc(tmp_path, "Cannot find device", rc=1)
    assert run("clear", "nope0", env_extra=env).returncode != 0


def test_show() -> None:
    assert run("show", "lo").stdout.strip() == "tc qdisc show dev lo"


@pytest.mark.parametrize(
    "args",
    [
        (),
        ("apply",),
        ("bogus", "lo"),
        ("apply", "lo", "jitter"),
        ("apply", "lo", "loss", "x"),
        ("apply", "lo", "loss", "101"),
    ],
)
def test_bad_usage_exits_2(args: tuple[str, ...]) -> None:
    assert run(*args).returncode == 2
