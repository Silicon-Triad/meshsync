"""Configuration: CLI arguments and config file.

Owner: Muhammad Ahmed Qazi (CS-24045)
Contract (PROJECT_CONTEXT.md §8.3):
    Flags from §14: --dir, --name, --secret-file, --tls / --plain, --cert-dir,
    --tcp-port, --peer HOST:PORT, --chunking {cdc,fixed}; plus optional config file.
Precedence: CLI flag > config file > default. Issue #6.
"""

import argparse
import logging
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from meshsync.protocol.constants import TCP_PORT, UDP_PORT

log = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path("~/.meshsync/config.toml")
CHUNKING_MODES = ("cdc", "fixed")  # D10
NAME_RE = re.compile(r"[a-z0-9_-]{1,32}")  # must equal the cert name (D9)

# Defaults for optional settings; required ones (sync_dir, name, secret_file) have none.
DEFAULTS: dict[str, Any] = {
    "tls": False,  # plain unless --tls (D9)
    "cert_dir": None,
    "tcp_port": TCP_PORT,  # §6.1
    "udp_port": UDP_PORT,  # §6.2
    "chunking": "cdc",  # D10
    "peers": [],  # static fallback list (§14)
    "log_level": "INFO",
}
FILE_KEYS = {"sync_dir", "name", "secret_file", *DEFAULTS}

# Allowed types per setting, so a wrong-typed file value gives ConfigError, not a crash.
# CLI values arrive as Path for the path settings.
PATH_TYPES = (str, Path)
TYPES: dict[str, tuple[type, ...]] = {
    "sync_dir": PATH_TYPES,
    "name": (str,),
    "secret_file": PATH_TYPES,
    "tls": (bool,),
    "cert_dir": PATH_TYPES,
    "tcp_port": (int,),
    "udp_port": (int,),
    "chunking": (str,),
    "peers": (list,),
    "log_level": (str,),
}


class ConfigError(ValueError):
    """Invalid or missing configuration."""


@dataclass(frozen=True)
class Config:
    sync_dir: Path
    name: str
    secret_file: Path
    tls: bool = False
    cert_dir: Path | None = None
    tcp_port: int = TCP_PORT
    udp_port: int = UDP_PORT
    chunking: str = "cdc"
    peers: tuple[tuple[str, int], ...] = ()
    log_level: str = "INFO"


def read_config_file(path: Path | None) -> dict[str, Any]:
    """Read the TOML config file. Missing default file is fine; missing explicit file is not."""
    explicit = path is not None
    path = (path or DEFAULT_CONFIG_PATH).expanduser()
    if not path.exists():
        if explicit:
            raise ConfigError(f"config file not found: {path}")
        return {}
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"bad config file {path}: {e}") from e
    unknown = set(data) - FILE_KEYS
    if unknown:
        raise ConfigError(f"unknown keys in {path}: {', '.join(sorted(unknown))}")
    return data


def merge(cli: dict[str, Any], file: dict[str, Any]) -> dict[str, Any]:
    """CLI flag > config file > default. A CLI value of None means "not given"."""
    merged: dict[str, Any] = {}
    for key in FILE_KEYS:
        if cli.get(key) is not None:
            merged[key] = cli[key]
        elif key in file:
            merged[key] = file[key]
        else:
            merged[key] = DEFAULTS.get(key)
    return merged


def check_types(c: dict[str, Any]) -> None:
    """Reject values of the wrong type, e.g. tls = "false" (a string, not a bool)."""
    for key, types in TYPES.items():
        value = c[key]
        if value is None:
            continue  # missing; required settings are checked separately
        if isinstance(value, bool) and bool not in types:
            ok = False  # bool is a subclass of int; tcp_port = true is not a port
        else:
            ok = isinstance(value, types)
        if key == "peers" and ok:
            ok = all(isinstance(p, str) for p in value)
        if not ok:
            raise ConfigError(f"{key} has the wrong type: {value!r}")


def parse_port(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise ConfigError(f"{what} must be an integer 1-65535, got {value!r}")
    return value


def parse_peer(text: Any) -> tuple[str, int]:
    """Parse "host:port" (§14 static peer fallback)."""
    if not isinstance(text, str):
        raise ConfigError(f"peer must be HOST:PORT, got {text!r}")
    # IPv4 or host names only; an IPv6 "[::1]:47001" would keep its brackets (peers use IPv4).
    host, sep, port = text.rpartition(":")
    if not sep or not host or not port.isdigit():
        raise ConfigError(f"peer must be HOST:PORT, got {text!r}")
    return host, parse_port(int(port), f"peer port in {text!r}")


def load_config(args: argparse.Namespace) -> Config:
    """Build and validate a Config from parsed CLI args plus the optional config file."""
    cli = {
        "sync_dir": args.dir,
        "name": args.name,
        "secret_file": args.secret_file,
        "tls": args.tls,
        "cert_dir": args.cert_dir,
        "tcp_port": args.tcp_port,
        "udp_port": args.udp_port,
        "chunking": args.chunking,
        "peers": args.peer,  # None unless --peer given at least once
        "log_level": args.log_level,
    }
    c = merge(cli, read_config_file(args.config))
    check_types(c)

    for key in ("sync_dir", "name", "secret_file"):
        if not c[key]:
            raise ConfigError(f"missing required setting: {key}")

    sync_dir = Path(c["sync_dir"]).expanduser()
    if not sync_dir.is_dir():
        raise ConfigError(f"sync dir does not exist or is not a directory: {sync_dir}")

    name = c["name"]
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise ConfigError(f"name must match [a-z0-9_-]{{1,32}}, got {name!r}")

    # Only the path is checked; the secret's contents are read by the auth code (#4).
    secret_file = Path(c["secret_file"]).expanduser()
    if not secret_file.is_file():
        raise ConfigError(f"secret file not found: {secret_file}")
    if secret_file.stat().st_mode & 0o077:
        log.warning("secret file %s is readable by others; run chmod 600 on it", secret_file)

    tls = c["tls"]
    cert_dir = Path(c["cert_dir"]).expanduser() if c["cert_dir"] else None
    if tls:  # D9: only check the files exist here; loading them is #5
        if cert_dir is None:
            raise ConfigError("--tls requires --cert-dir")
        for fname in ("ca.crt", f"{name}.crt", f"{name}.key"):
            if not (cert_dir / fname).is_file():
                raise ConfigError(f"--tls: missing {cert_dir / fname}")

    if c["chunking"] not in CHUNKING_MODES:
        raise ConfigError(f"chunking must be one of {CHUNKING_MODES}, got {c['chunking']!r}")

    level = c["log_level"].upper()
    if not isinstance(logging.getLevelName(level), int):
        raise ConfigError(f"unknown log level: {c['log_level']!r}")

    return Config(
        sync_dir=sync_dir,
        name=name,
        secret_file=secret_file,
        tls=tls,
        cert_dir=cert_dir,
        tcp_port=parse_port(c["tcp_port"], "tcp_port"),
        udp_port=parse_port(c["udp_port"], "udp_port"),
        chunking=c["chunking"],
        peers=tuple(parse_peer(p) for p in c["peers"]),
        log_level=level,
    )
