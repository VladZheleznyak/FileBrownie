"""Erase scopes (D11). Generated data only: source documents are never touched.

Derived data (generations and everything they own, caches, evidence rasters, proposals, logs)
is regenerable. User decisions (dictionary reviews, manual checks) go only with `erase all`.
Database deletion commits before filesystem cleanup; a cleanup failure leaves
`ERASE_CLEANUP_INCOMPLETE` and generated files may remain until the same erase
scope is run again.
"""

import shutil
from dataclasses import dataclass
from pathlib import Path

from psycopg import Connection

CONFIRMATION_PHRASE = "erase all"
DERIVED_DIRECTORIES = ("evidence", "logs")  # inside the generated-data folder


class ErasureError(Exception):
    """Fixed safe codes only."""


@dataclass(frozen=True)
class ErasureResult:
    generations: int
    cache_entries: int
    directories: int
    decisions: int = 0
    checks: int = 0


def _clear_directory(data: Path, name: str) -> bool:
    target = data / name
    if target.is_symlink():
        raise ErasureError("ERASE_REFUSED_UNSAFE_PATH")
    if not target.exists():
        return False
    if not target.is_dir() or not target.resolve().is_relative_to(data.resolve()):
        raise ErasureError("ERASE_REFUSED_UNSAFE_PATH")
    for child in target.iterdir():
        if child.is_symlink() or child.is_file():
            child.unlink()
        else:
            shutil.rmtree(child)
    return True


def _derived_rows(connection: Connection) -> tuple[int, int]:
    generations = connection.execute("SELECT count(*) AS total FROM generations").fetchone()
    cache = 0
    connection.execute("DELETE FROM active_generation")
    connection.execute("DELETE FROM generations")  # cascades to everything a generation owns
    for table in ("step_cache", "reader_cache"):
        cache += connection.execute(f"DELETE FROM {table}").rowcount
    connection.execute("DELETE FROM contents")
    return generations["total"], cache


def erase_derived(connection: Connection, data: Path) -> ErasureResult:
    # Validate every path first so a refusal leaves the database untouched.
    for name in DERIVED_DIRECTORIES:
        if (data / name).is_symlink():
            raise ErasureError("ERASE_REFUSED_UNSAFE_PATH")
    with connection.transaction():
        generations, cache = _derived_rows(connection)
    try:
        removed = sum(_clear_directory(data, name) for name in DERIVED_DIRECTORIES)
    except OSError:
        raise ErasureError("ERASE_CLEANUP_INCOMPLETE") from None
    return ErasureResult(generations, cache, removed)


def erase_all(connection: Connection, data: Path, phrase: str) -> ErasureResult:
    if phrase.strip() != CONFIRMATION_PHRASE:
        raise ErasureError("ERASE_CONFIRMATION_MISMATCH")
    for name in DERIVED_DIRECTORIES:
        if (data / name).is_symlink():
            raise ErasureError("ERASE_REFUSED_UNSAFE_PATH")
    with connection.transaction():
        generations, cache = _derived_rows(connection)
        decisions = connection.execute("DELETE FROM term_decisions").rowcount
        checks = connection.execute("DELETE FROM manual_checks").rowcount
        connection.execute("UPDATE dictionary_state SET revision = revision + 1")
    try:
        removed = sum(_clear_directory(data, name) for name in DERIVED_DIRECTORIES)
    except OSError:
        raise ErasureError("ERASE_CLEANUP_INCOMPLETE") from None
    return ErasureResult(generations, cache, removed, decisions, checks)
