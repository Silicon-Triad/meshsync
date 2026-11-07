"""CLI entry point: meshsync run / status / stats (§14).

Owner: Muhammad Ahmed Qazi (CS-24045)
Status: argument parsing and config loading done (#6); node wiring comes later.
"""

import argparse
import logging
import sys
from pathlib import Path

from meshsync.config import CHUNKING_MODES, ConfigError, load_config

log = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="meshsync", description="Serverless P2P file sync.")
    sub = parser.add_subparsers(dest="command", required=True)

    # Every default is None so load_config can tell "not given" from a value (CLI > file).
    run = sub.add_parser("run", help="run this peer")
    run.add_argument("--dir", type=Path, help="shared folder to sync (must exist)")
    run.add_argument("--name", help="peer name, equal to its cert name")
    run.add_argument("--secret-file", type=Path, help="file holding the group secret")
    mode = run.add_mutually_exclusive_group()
    mode.add_argument("--tls", dest="tls", action="store_const", const=True, help="TLS 1.3 (D9)")
    mode.add_argument("--plain", dest="tls", action="store_const", const=False, help="no TLS")
    run.add_argument("--cert-dir", type=Path, help="dir with ca.crt, NAME.crt, NAME.key")
    run.add_argument("--tcp-port", type=int, help="TCP data port (default 47001)")
    run.add_argument("--udp-port", type=int, help="UDP discovery port (default 47000)")
    run.add_argument("--chunking", choices=CHUNKING_MODES, help="chunking mode (default cdc)")
    run.add_argument(
        "--peer", action="append", metavar="HOST:PORT", help="static peer (repeatable)"
    )
    run.add_argument("--config", type=Path, help="config file (default ~/.meshsync/config.toml)")
    run.add_argument("--log-level", help="DEBUG, INFO, WARNING, ERROR (default INFO)")

    sub.add_parser("status", help="show peers and sync state")
    sub.add_parser("stats", help="show transfer statistics")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command != "run":
        print(f"meshsync {args.command}: not implemented yet", file=sys.stderr)
        return 1

    try:
        config = load_config(args)
    except ConfigError as e:
        print(f"meshsync: config error: {e}", file=sys.stderr)
        return 2

    logging.basicConfig(
        level=config.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    log.info("config: %s", config)  # holds the secret's path only, never its contents
    print("meshsync run: node not implemented yet (see wiring issue)", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
