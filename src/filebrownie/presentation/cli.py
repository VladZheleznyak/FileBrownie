"""Scaffold entry point; no source documents are read by these commands."""

import argparse
import sys
from collections.abc import Sequence

from filebrownie import __version__


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="filebrownie",
        description="Local medical histories. Scaffold only; processing is not implemented.",
    )
    parser.add_argument("--version", action="version", version=f"FileBrownie {__version__}")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("status", help="Show implementation status (does not inspect data).")
    commands.add_parser("model-service", help="Reserved local inference service entry point.")
    commands.add_parser("model-setup", help="Reserved isolated model provisioning entry point.")
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "status":
        print(
            "Scaffold only. No scan, extraction, history, or database integration is implemented."
        )
        return 0
    print(
        "SETUP_NOT_IMPLEMENTED: OCR/vision baseline and versioned provisioning are pending. "
        "See docs/implementation-checklist.md.",
        file=sys.stderr,
    )
    return 2
