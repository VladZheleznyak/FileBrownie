"""English CLI output; source references are intentional output, never logs."""

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from filebrownie import __version__
from filebrownie.ingestion.discovery import DiscoveryStatus, InventoryError, discover_sources
from filebrownie.storage.operation import OperationBusyError, OperationLockError, operation_lock


def inventory() -> int:
    source = Path(os.environ.get("FILEBROWNIE_SOURCE_DIR", "/sources"))
    data = Path(os.environ.get("FILEBROWNIE_DATA_DIR", "/data"))
    try:
        if not source.is_absolute() or not data.is_absolute():
            raise InventoryError("ABSOLUTE_DIRECTORIES_REQUIRED")
        source_resolved, data_resolved = source.resolve(), data.resolve()
        if source_resolved.is_relative_to(data_resolved) or data_resolved.is_relative_to(
            source_resolved
        ):
            raise InventoryError("SOURCE_DATA_OVERLAP")
        with operation_lock(data):
            result = discover_sources(source)
            print(f"Inventory timestamp: {result.created_at.isoformat()}")
            print(f"Supported files: {result.supported_file_count}")
            print(f"Unsupported files: {result.unsupported_file_count}")
            print(f"Unique fingerprinted contents: {len(result.content_sources())}")
            print("Discovery only. Documents were not parsed; extraction coverage is unknown.")
            print("Status\tFormat\tSource\tWarnings")
            for record in result.records:
                # Escape controls and Unicode formatting characters in terminal filenames.
                print(
                    f"{record.status}\t{record.format or '-'}\t{ascii(record.relative_path)}\t"
                    f"{', '.join(record.warnings) or '-'}"
                )
            incomplete = any(
                record.status in (DiscoveryStatus.FAILED, DiscoveryStatus.SKIPPED)
                for record in result.records
            )
            return 1 if incomplete else 0
    except (InventoryError, OperationLockError, OperationBusyError) as error:
        print(str(error), file=sys.stderr)
        return 2
    except OSError:
        print("INVENTORY_UNAVAILABLE", file=sys.stderr)
        return 2


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="filebrownie",
        description=(
            "Local medical histories. Source inventory is available; extraction is pending."
        ),
    )
    parser.add_argument("--version", action="version", version=f"FileBrownie {__version__}")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("status", help="Show implementation status (does not inspect data).")
    commands.add_parser("inventory", help="List and fingerprint sources without parsing documents.")
    commands.add_parser("model-service", help="Reserved local inference service entry point.")
    commands.add_parser("model-setup", help="Reserved isolated model provisioning entry point.")
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "status":
        print(
            "Source inventory is available. "
            "No scan, extraction, history, or database integration is implemented."
        )
        return 0
    if args.command == "inventory":
        return inventory()
    print(
        "SETUP_NOT_IMPLEMENTED: OCR/vision baseline and versioned provisioning are pending. "
        "See docs/implementation-checklist.md.",
        file=sys.stderr,
    )
    return 2
