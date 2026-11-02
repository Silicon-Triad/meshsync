"""CLI entry point: meshsync run / status / stats.

Owner: Muhammad Ahmed Qazi (CS-24045)
Status: skeleton; subcommands not implemented.
"""

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="meshsync", description="Serverless P2P file sync.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="run this peer")
    sub.add_parser("status", help="show peers and sync state")
    sub.add_parser("stats", help="show transfer statistics")
    args = parser.parse_args(argv)
    print(f"meshsync {args.command}: not implemented yet", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
