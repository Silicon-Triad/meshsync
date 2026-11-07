"""Tests for config loading and CLI parsing (#6). No real home dir: --config always given."""

import subprocess
import sys
from pathlib import Path

import pytest

from meshsync.__main__ import build_parser, main
from meshsync.config import ConfigError, load_config
from meshsync.protocol.constants import TCP_PORT, UDP_PORT


@pytest.fixture
def env(tmp_path: Path) -> dict[str, Path]:
    sync = tmp_path / "sync"
    sync.mkdir()
    secret = tmp_path / "key"
    secret.write_text("s3cret")
    secret.chmod(0o600)
    cfg = tmp_path / "config.toml"
    cfg.write_text("")
    return {"tmp": tmp_path, "dir": sync, "secret": secret, "cfg": cfg}


def args_for(env: dict[str, Path], *extra: str, required: bool = True) -> list[str]:
    argv = ["run", "--config", str(env["cfg"])]
    if required:
        argv += ["--dir", str(env["dir"]), "--name", "ahmed", "--secret-file", str(env["secret"])]
    return argv + list(extra)


def load(argv: list[str]):
    return load_config(build_parser().parse_args(argv))


def test_defaults(env):
    c = load(args_for(env))
    assert c.sync_dir == env["dir"] and c.name == "ahmed" and c.secret_file == env["secret"]
    assert c.tls is False and c.cert_dir is None
    assert (c.tcp_port, c.udp_port) == (TCP_PORT, UDP_PORT)
    assert c.chunking == "cdc" and c.peers == () and c.log_level == "INFO"


def test_file_values_used_and_cli_overrides(env):
    env["cfg"].write_text(
        f'sync_dir = "{env["dir"]}"\nname = "mujtaba"\nsecret_file = "{env["secret"]}"\n'
        'tcp_port = 47002\nchunking = "fixed"\npeers = ["10.0.0.2:47001"]\n'
    )
    c = load(args_for(env, required=False))
    assert c.name == "mujtaba" and c.tcp_port == 47002 and c.chunking == "fixed"
    assert c.peers == (("10.0.0.2", 47001),)
    c = load(args_for(env, required=False) + ["--tcp-port", "47003", "--peer", "h:1"])
    assert c.tcp_port == 47003 and c.peers == (("h", 1),) and c.chunking == "fixed"


def test_missing_explicit_config_file(env):
    env["cfg"].unlink()
    with pytest.raises(ConfigError):
        load(args_for(env))


@pytest.mark.parametrize("drop", ["--dir", "--name", "--secret-file"])
def test_missing_required(env, drop):
    argv = args_for(env)
    i = argv.index(drop)
    del argv[i : i + 2]
    with pytest.raises(ConfigError):
        load(argv)


def test_nonexistent_dir_and_secret(env):
    with pytest.raises(ConfigError):
        load(args_for(env, "--dir", str(env["tmp"] / "nope")))
    with pytest.raises(ConfigError):
        load(args_for(env, "--secret-file", str(env["tmp"] / "nope")))


def test_tls_requires_certs(env):
    certs = env["tmp"] / "certs"
    certs.mkdir()
    with pytest.raises(ConfigError):
        load(args_for(env, "--tls"))
    with pytest.raises(ConfigError):
        load(args_for(env, "--tls", "--cert-dir", str(certs)))
    for f in ("ca.crt", "ahmed.crt", "ahmed.key"):
        (certs / f).write_text("x")
    c = load(args_for(env, "--tls", "--cert-dir", str(certs)))
    assert c.tls is True and c.cert_dir == certs


@pytest.mark.parametrize(
    "extra",
    [
        ["--name", "Ahmed!"],
        ["--name", "a" * 33],
        ["--tcp-port", "0"],
        ["--udp-port", "65536"],
        ["--peer", "nohost"],
        ["--peer", "host:abc"],
        ["--peer", "host:70000"],
    ],
)
def test_bad_values(env, extra):
    with pytest.raises(ConfigError):
        load(args_for(env, *extra))


def test_tls_and_plain_exclusive(env):
    with pytest.raises(SystemExit) as e:
        build_parser().parse_args(args_for(env, "--tls", "--plain"))
    assert e.value.code == 2


def test_main_exit_codes(env):
    assert main(args_for(env)) == 1
    assert main(args_for(env, "--name", "BAD")) == 2
    assert main(["status"]) == 1


@pytest.mark.parametrize(
    "line",
    [
        'tls = "false"',
        "sync_dir = 5",
        "name = 7",
        "secret_file = true",
        "cert_dir = 1",
        'tcp_port = "47001"',
        "udp_port = true",
        "chunking = 1",
        'peers = "h:1"',
        "peers = [1]",
        "log_level = 10",
    ],
)
def test_wrong_types_in_file(env, line):
    env["cfg"].write_text(line + "\n")
    with pytest.raises(ConfigError):
        load(args_for(env, required=False))  # no CLI flags, so the file value is used


def test_tls_bool_in_file(env):
    env["cfg"].write_text("tls = false\n")
    assert load(args_for(env)).tls is False


def test_unknown_file_key(env):
    env["cfg"].write_text("colour = 1\n")
    with pytest.raises(ConfigError):
        load(args_for(env))


def test_secret_permission_warning_formatted(env):
    # Subprocess: pytest's own log handler would hide the CLI's logging.basicConfig format.
    env["secret"].chmod(0o644)
    r = subprocess.run(
        [sys.executable, "-m", "meshsync", *args_for(env)], capture_output=True, text=True
    )
    assert r.returncode == 1
    assert " WARNING meshsync.config: secret file" in r.stderr
