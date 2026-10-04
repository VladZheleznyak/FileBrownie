"""Inventory/fingerprint comparison for later scan activation; never resolves gaps."""

from dataclasses import dataclass

from filebrownie.ingestion.discovery import SourceInventory


@dataclass(frozen=True)
class InventoryDifference:
    added: tuple[str, ...]
    removed: tuple[str, ...]
    changed: tuple[str, ...]
    unverifiable: tuple[str, ...]

    @property
    def consistent(self) -> bool:
        return not (self.added or self.removed or self.changed or self.unverifiable)


def compare_inventories(saved: SourceInventory, current: SourceInventory) -> InventoryDifference:
    before = {record.relative_path: record for record in saved.records}
    after = {record.relative_path: record for record in current.records}
    shared = before.keys() & after.keys()
    return InventoryDifference(
        added=tuple(sorted(after.keys() - before.keys())),
        removed=tuple(sorted(before.keys() - after.keys())),
        changed=tuple(sorted(path for path in shared if before[path] != after[path])),
        unverifiable=tuple(
            sorted(
                path
                for path in shared
                if before[path].content_hash is None or after[path].content_hash is None
            )
        ),
    )
