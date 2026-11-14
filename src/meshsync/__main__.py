"""CLI entry point: meshsync run / status / stats (§14).

Owner: Muhammad Ahmed Qazi (CS-24045)
Status: argument parsing, config loading (#6) and node wiring (#40) done.
"""

import argparse
import logging
import sys
import threading
from pathlib import Path

from meshsync.config import CHUNKING_MODES, Config, ConfigError, load_config
from meshsync.metrics.logger import CSV_HEADER, CSV_LOGGER
from meshsync.node import ComponentsMissing, build_node
from meshsync.protocol.constants import TCP_PORT, UDP_PORT

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
    run.add_argument("--tcp-port", type=int, help=f"TCP data port (default {TCP_PORT})")
    run.add_argument("--udp-port", type=int, help=f"UDP discovery port (default {UDP_PORT})")
    run.add_argument("--chunking", choices=CHUNKING_MODES, help="chunking mode (default cdc)")
    run.add_argument(
        "--peer", action="append", metavar="HOST:PORT", help="static peer (repeatable)"
    )
    run.add_argument("--config", type=Path, help="config file (default ~/.meshsync/config.toml)")
    run.add_argument("--log-level", help="DEBUG, INFO, WARNING, ERROR (default INFO)")
    run.add_argument(
        "--metrics-csv",
        type=Path,
        metavar="PATH",
        help="write one CSV row per message to PATH, overwriting it (default off; §11)",
    )

    sub.add_parser("status", help="show peers and sync state")
    sub.add_parser("stats", help="show transfer statistics")
    return parser


def open_metrics_csv(path: Path) -> logging.FileHandler:
    """Overwrite PATH, write CSV_HEADER, attach a handler to the metrics CSV logger (§11, #72).

    One fresh file per run, as §11 stores one file per run; the caller removes the handler.
    """
    try:
        handler = logging.FileHandler(path, mode="w", encoding="utf-8")
    except OSError as e:
        raise ConfigError(f"cannot open metrics CSV {path}: {e.strerror or e}") from e
    handler.stream.write(CSV_HEADER + "\n")
    handler.stream.flush()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger(CSV_LOGGER).addHandler(handler)
    return handler


def close_metrics_csv(handler: logging.FileHandler | None) -> None:
    if handler is not None:
        logging.getLogger(CSV_LOGGER).removeHandler(handler)
        handler.close()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command != "run":
        print(f"meshsync {args.command}: not implemented yet", file=sys.stderr)
        return 1

    # Set the log format first so warnings from load_config look like every other line;
    # the level is applied once the config is known.
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        config = load_config(args)
        metrics = open_metrics_csv(config.metrics_csv) if config.metrics_csv else None
    except ConfigError as e:
        print(f"meshsync: config error: {e}", file=sys.stderr)
        return 2

    logging.getLogger().setLevel(config.log_level)
    log.info("config: %s", config)  # holds the secret's path only, never its contents
    try:
        return run_node(config)
    finally:
        close_metrics_csv(metrics)


def run_node(config: Config) -> int:
    try:
        node = build_node(config)
    except ComponentsMissing as e:
        log.debug("components missing: %s", e)
        print(
            "meshsync: sync/transfer modules not implemented yet (issues #9-#16)", file=sys.stderr
        )
        return 1
    node.start()
    try:
        threading.Event().wait()  # run until Ctrl-C
    except KeyboardInterrupt:
        pass
    finally:
        node.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
