"""Inventory/fingerprint comparison for later scan activation; never resolves gaps."""

from dataclasses import dataclass

from filebrownie.ingestion.discovery import SourceInventory, SourceRecord


@dataclass(frozen=True)
class InventoryDifference:
    added: tuple[str, ...]
    removed: tuple[str, ...]
    changed: tuple[str, ...]
    unverifiable: tuple[str, ...]
    changed_details: tuple[tuple[str, str], ...] = ()

    @property
    def consistent(self) -> bool:
        return not (self.added or self.removed or self.changed or self.unverifiable)


def _identity(record: SourceRecord) -> tuple:
    return (
        record.status,
        record.content_hash,
        record.kind,
        record.format,
    )


def _explain_change(before: SourceRecord, after: SourceRecord) -> str:
    parts: list[str] = []
    if before.status != after.status:
        parts.append(f"status {before.status.value}->{after.status.value}")
    if before.content_hash != after.content_hash:
        if before.content_hash and after.content_hash:
            parts.append("content_hash")
        elif before.content_hash is None:
            parts.append("fingerprint missing")
        else:
            parts.append("fingerprint added")
    if before.format != after.format:
        parts.append("format")
    for warning in after.warnings:
        if warning.startswith("fields:"):
            parts.append(warning)
    return "; ".join(parts) if parts else "record metadata"


def compare_inventories(saved: SourceInventory, current: SourceInventory) -> InventoryDifference:
    before = {record.relative_path: record for record in saved.records}
    after = {record.relative_path: record for record in current.records}
    shared = before.keys() & after.keys()
    changed: list[str] = []
    details: list[tuple[str, str]] = []
    for path in sorted(shared):
        left, right = before[path], after[path]
        if _identity(left) != _identity(right):
            changed.append(path)
            details.append((path, _explain_change(left, right)))
    return InventoryDifference(
        added=tuple(sorted(after.keys() - before.keys())),
        removed=tuple(sorted(before.keys() - after.keys())),
        changed=tuple(changed),
        unverifiable=tuple(
            sorted(
                path
                for path in shared
                if before[path].content_hash is None or after[path].content_hash is None
            )
        ),
        changed_details=tuple(details),
    )
