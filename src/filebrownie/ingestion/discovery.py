"""Read-only source inventory. No document readers or archive expansion run here."""

import hashlib
import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

CHUNK_SIZE = 1024 * 1024
MAX_DIRECTORY_DEPTH = 64

_STAT_FIELD_NAMES = ("device", "inode", "mode", "size", "mtime_ns", "ctime_ns")
_VOLATILE_STAT_FIELDS = frozenset({"mtime_ns", "ctime_ns"})


class SourceFormat(StrEnum):
    PDF = "pdf"
    JPEG = "jpeg"


class DiscoveryStatus(StrEnum):
    READY = "ready"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class SourceRecord:
    relative_path: str
    kind: str
    format: SourceFormat | None
    status: DiscoveryStatus
    content_hash: str | None = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class SourceInventory:
    created_at: datetime
    records: tuple[SourceRecord, ...]

    @property
    def supported_file_count(self) -> int:
        return sum(record.kind == "file" and record.format is not None for record in self.records)

    @property
    def unsupported_file_count(self) -> int:
        return sum(record.kind == "file" and record.format is None for record in self.records)

    @property
    def ready_file_count(self) -> int:
        return sum(
            record.kind == "file"
            and record.status == DiscoveryStatus.READY
            and record.content_hash is not None
            for record in self.records
        )

    def content_sources(self) -> dict[str, tuple[str, ...]]:
        """Retain every path for identical bytes; no medical interpretation occurs."""
        groups: dict[str, list[str]] = {}
        for record in self.records:
            if record.content_hash is not None:
                groups.setdefault(record.content_hash, []).append(record.relative_path)
        return {digest: tuple(paths) for digest, paths in groups.items()}


class InventoryError(Exception):
    """A sanitized operational failure; never includes source paths or raw errors."""


class SourceChangedError(Exception):
    """File or directory identity changed during discovery."""

    def __init__(self, fields: tuple[str, ...] = ()) -> None:
        self.fields = fields
        super().__init__()


def source_format(name: str) -> SourceFormat | None:
    match Path(name).suffix.casefold():
        case ".pdf":
            return SourceFormat.PDF
        case ".jpg" | ".jpeg":
            return SourceFormat.JPEG
        case _:
            return None


def _signature(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def describe_stat_changes(before: os.stat_result, after: os.stat_result) -> tuple[str, ...]:
    """Human-readable field deltas for CLI output; never includes paths."""
    changes: list[str] = []
    for name, left, right in zip(
        _STAT_FIELD_NAMES, _signature(before), _signature(after), strict=True
    ):
        if left == right:
            continue
        if name in _VOLATILE_STAT_FIELDS:
            changes.append(f"{name} {left}->{right}")
        elif name == "size":
            changes.append(f"size {left}->{right}")
        else:
            changes.append(name)
    return tuple(changes)


def _fields_warning(fields: tuple[str, ...]) -> tuple[str, ...]:
    if not fields:
        return ("SOURCE_CHANGED",)
    return ("SOURCE_CHANGED", f"fields: {', '.join(fields)}")


def _require_file_identity(initial: os.stat_result, current: os.stat_result) -> None:
    """Reject identity or content-size changes; allow timestamp-only drift (shared drives)."""
    fields = describe_stat_changes(initial, current)
    blocking = tuple(
        item
        for item in fields
        if not item.startswith("mtime_ns") and not item.startswith("ctime_ns")
    )
    if blocking:
        raise SourceChangedError(blocking)


def _require_directory_identity(initial: os.stat_result, current: os.stat_result) -> None:
    fields = describe_stat_changes(initial, current)
    blocking = tuple(
        item
        for item in fields
        if not item.startswith("mtime_ns") and not item.startswith("ctime_ns")
    )
    if blocking:
        raise SourceChangedError(blocking)


def _fingerprint(directory_fd: int, name: str, initial: os.stat_result) -> tuple[str, tuple[str, ...]]:
    # A directory descriptor prevents parent-directory replacement redirecting reads.
    # NOFOLLOW rejects a swapped symlink; NONBLOCK prevents a swapped FIFO from hanging.
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    touched: tuple[str, ...] = ()
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode):
            raise SourceChangedError(("mode",))
        _require_file_identity(initial, opened)
        digest = hashlib.sha256()
        size_before = opened.st_size
        while chunk := stream.read(CHUNK_SIZE):
            digest.update(chunk)
        after = os.fstat(stream.fileno())
        if after.st_size != size_before:
            raise SourceChangedError((f"size {size_before}->{after.st_size}",))
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        _require_file_identity(opened, current)
        volatile = describe_stat_changes(opened, after) + describe_stat_changes(after, current)
        touched = tuple(
            item
            for item in volatile
            if item.startswith("mtime_ns") or item.startswith("ctime_ns")
        )
    return digest.hexdigest(), touched


def discover_sources(root: Path) -> SourceInventory:
    """Recursively enumerate one folder, hashing files without parsing their contents.

    Detected changes while reading fail that record. This is not an atomic snapshot;
    future scan activation must revalidate the full inventory and fingerprints.
    """
    records: list[SourceRecord] = []

    def failure(
        path: str,
        kind: str,
        code: str,
        format: SourceFormat | None = None,
        *,
        fields: tuple[str, ...] = (),
    ) -> None:
        warnings = (code,) if code != "SOURCE_CHANGED" else _fields_warning(fields)
        records.append(SourceRecord(path, kind, format, DiscoveryStatus.FAILED, warnings=warnings))

    def walk(directory_fd: int, relative: Path, depth: int) -> None:
        try:
            with os.scandir(directory_fd) as entries:
                names = sorted(entry.name for entry in entries)
        except OSError:
            failure(relative.as_posix(), "directory", "DIRECTORY_UNREADABLE")
            return
        for name in names:
            path = relative / name
            label = path.as_posix()
            try:
                metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError:
                failure(label, "unknown", "SOURCE_UNREADABLE")
                continue
            if stat.S_ISLNK(metadata.st_mode):
                records.append(
                    SourceRecord(
                        label,
                        "symlink",
                        None,
                        DiscoveryStatus.SKIPPED,
                        warnings=("SYMLINK_NOT_FOLLOWED",),
                    )
                )
            elif stat.S_ISDIR(metadata.st_mode):
                if depth >= MAX_DIRECTORY_DEPTH:
                    failure(label, "directory", "DIRECTORY_DEPTH_LIMIT")
                    continue
                try:
                    child_fd = os.open(
                        name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd
                    )
                except OSError:
                    failure(label, "directory", "DIRECTORY_UNREADABLE")
                    continue
                try:
                    try:
                        _require_directory_identity(metadata, os.fstat(child_fd))
                    except SourceChangedError as error:
                        failure(label, "directory", "SOURCE_CHANGED", fields=error.fields)
                    else:
                        walk(child_fd, path, depth + 1)
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(metadata.st_mode):
                format = source_format(name)
                try:
                    digest, touched = _fingerprint(directory_fd, name, metadata)
                except SourceChangedError as error:
                    failure(label, "file", "SOURCE_CHANGED", format, fields=error.fields)
                except OSError:
                    failure(label, "file", "SOURCE_UNREADABLE", format)
                else:
                    status = DiscoveryStatus.READY if format else DiscoveryStatus.UNSUPPORTED
                    warnings: tuple[str, ...] = ()
                    if touched:
                        warnings = ("METADATA_TOUCHED", f"fields: {', '.join(touched)}")
                    records.append(
                        SourceRecord(label, "file", format, status, digest, warnings)
                    )
            else:
                records.append(
                    SourceRecord(
                        label,
                        "special",
                        None,
                        DiscoveryStatus.SKIPPED,
                        warnings=("SPECIAL_FILE_NOT_READ",),
                    )
                )

    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise InventoryError("SOURCE_FOLDER_UNAVAILABLE") from None
    try:
        walk(root_fd, Path(), 0)
    finally:
        os.close(root_fd)
    return SourceInventory(
        datetime.now(UTC), tuple(sorted(records, key=lambda item: item.relative_path))
    )
