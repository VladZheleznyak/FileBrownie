"""English CLI output; source references are intentional output, never logs."""

import argparse
import os
import sys
from collections.abc import Sequence
from contextlib import nullcontext
from pathlib import Path
from uuid import UUID

from filebrownie import __version__
from filebrownie.ingestion.discovery import DiscoveryStatus, InventoryError, discover_sources
from filebrownie.storage.database import DatabaseError, open_repository
from filebrownie.storage.operation import OperationBusyError, OperationLockError, operation_lock


def configured_paths() -> tuple[Path, Path]:
    source = Path(os.environ.get("FILEBROWNIE_SOURCE_DIR", "/sources"))
    data = Path(os.environ.get("FILEBROWNIE_DATA_DIR", "/data"))
    if not source.is_absolute() or not data.is_absolute():
        raise InventoryError("ABSOLUTE_DIRECTORIES_REQUIRED")
    source_resolved, data_resolved = source.resolve(), data.resolve()
    if source_resolved.is_relative_to(data_resolved) or data_resolved.is_relative_to(
        source_resolved
    ):
        raise InventoryError("SOURCE_DATA_OVERLAP")
    return source, data


def inventory(save: bool = False) -> int:
    try:
        source, data = configured_paths()
        with operation_lock(data):
            with open_repository() if save else nullcontext() as repository:
                generation_id = None
                if repository is not None:
                    repository.require_schema()
                    repository.recover_interrupted()
                    generation_id = repository.begin_inventory()
                try:
                    result = discover_sources(source)
                except (InventoryError, OSError, KeyboardInterrupt):
                    if repository is not None:
                        repository.interrupt(generation_id)
                    raise
                if repository is not None:
                    repository.save_inventory(generation_id, result)
                    print(f"Generation: {generation_id} (staged; inventory only)")
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
    except (InventoryError, OperationLockError, OperationBusyError, DatabaseError) as error:
        print(str(error), file=sys.stderr)
        return 2
    except OSError:
        print("INVENTORY_UNAVAILABLE", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("OPERATION_INTERRUPTED", file=sys.stderr)
        return 130


def database_command(command: str, generation_id: UUID | None = None) -> int:
    try:
        source, data = configured_paths()
        with operation_lock(data), open_repository() as repository:
            if command == "migrate":
                repository.migrate()
                print("Database schema is current.")
                return 0
            repository.require_schema()
            recovered = repository.recover_interrupted()
            if recovered:
                print(f"Interrupted inventory generations recovered: {recovered}")
            if command == "status":
                print("Active medical index: none (extraction pipeline is pending).")
                print("Generation\tState\tStarted at\tSources\tReason")
                for generation in repository.generations():
                    print(
                        f"{generation.id}\t{generation.state}\t{generation.started_at.isoformat()}"
                        f"\t{generation.source_count}\t{generation.reason}"
                    )
                return 0
            repository.load_inventory(generation_id)
            difference = repository.revalidate(generation_id, discover_sources(source))
            for label in ("added", "removed", "changed", "unverifiable"):
                paths = getattr(difference, label)
                print(f"{label.capitalize()}: {len(paths)}")
                for path in paths:
                    print(f"  {ascii(path)}")
            generation = next(item for item in repository.generations() if item.id == generation_id)
            print(f"Generation state: {generation.state}")
            print("Inventory only; this does not activate a medical index or establish coverage.")
            return 0 if difference.consistent and generation.state == "staged" else 1
    except (InventoryError, OperationLockError, OperationBusyError, DatabaseError) as error:
        print(str(error), file=sys.stderr)
        return 2
    except OSError:
        print("OPERATION_UNAVAILABLE", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("OPERATION_INTERRUPTED", file=sys.stderr)
        return 130


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="filebrownie",
        description=(
            "Local medical histories. Source inventory is available; extraction is pending."
        ),
    )
    parser.add_argument("--version", action="version", version=f"FileBrownie {__version__}")
    commands = parser.add_subparsers(dest="command")
    status = commands.add_parser("status", help="Show implementation or saved-generation status.")
    status.add_argument("--database", action="store_true", help="Inspect local saved generations.")
    sources = commands.add_parser("inventory", help="List sources without parsing documents.")
    sources.add_argument(
        "--save", action="store_true", help="Persist a staged inventory generation."
    )
    commands.add_parser("migrate", help="Initialize or upgrade the local database schema.")
    validate = commands.add_parser("validate", help="Recheck a saved inventory against sources.")
    validate.add_argument("generation", type=UUID)
    commands.add_parser("model-service", help="Reserved local inference service entry point.")
    commands.add_parser("model-setup", help="Reserved isolated model provisioning entry point.")
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "status":
        if args.database:
            return database_command("status")
        print(
            "Source inventory and staged database generations are available. "
            "No scan, extraction, or history pipeline is implemented."
        )
        return 0
    if args.command == "inventory":
        return inventory(args.save)
    if args.command == "migrate":
        return database_command("migrate")
    if args.command == "validate":
        return database_command("validate", args.generation)
    print(
        "SETUP_NOT_IMPLEMENTED: OCR/vision baseline and versioned provisioning are pending. "
        "See docs/implementation-checklist.md.",
        file=sys.stderr,
    )
    return 2
