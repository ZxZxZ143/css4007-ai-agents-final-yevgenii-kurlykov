"""Command line entry point."""

import argparse

from deskpilot import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="deskpilot", description="Policy-gated desktop agent.")
    parser.add_argument("--version", action="version", version=f"deskpilot {__version__}")
    parser.add_subparsers(dest="command", title="commands")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
    return 0
