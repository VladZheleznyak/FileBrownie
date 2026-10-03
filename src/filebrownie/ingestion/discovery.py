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
    pass


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


def _fingerprint(directory_fd: int, name: str, initial: os.stat_result) -> str:
    # A directory descriptor prevents parent-directory replacement redirecting reads.
    # NOFOLLOW rejects a swapped symlink; NONBLOCK prevents a swapped FIFO from hanging.
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or _signature(before) != _signature(initial):
            raise SourceChangedError
        digest = hashlib.sha256()
        while chunk := stream.read(CHUNK_SIZE):
            digest.update(chunk)
        after = os.fstat(stream.fileno())
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if _signature(before) != _signature(after) or _signature(after) != _signature(current):
            raise SourceChangedError
    return digest.hexdigest()


def discover_sources(root: Path) -> SourceInventory:
    """Recursively enumerate one folder, hashing files without parsing their contents.

    Detected changes while reading fail that record. This is not an atomic snapshot;
    future scan activation must revalidate the full inventory and fingerprints.
    """
    records: list[SourceRecord] = []

    def failure(path: str, kind: str, code: str, format: SourceFormat | None = None) -> None:
        records.append(SourceRecord(path, kind, format, DiscoveryStatus.FAILED, warnings=(code,)))

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
                    if _signature(os.fstat(child_fd)) != _signature(metadata):
                        failure(label, "directory", "SOURCE_CHANGED")
                    else:
                        walk(child_fd, path, depth + 1)
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(metadata.st_mode):
                format = source_format(name)
                try:
                    digest = _fingerprint(directory_fd, name, metadata)
                except SourceChangedError:
                    failure(label, "file", "SOURCE_CHANGED", format)
                except OSError:
                    failure(label, "file", "SOURCE_UNREADABLE", format)
                else:
                    status = DiscoveryStatus.READY if format else DiscoveryStatus.UNSUPPORTED
                    records.append(SourceRecord(label, "file", format, status, digest))
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
