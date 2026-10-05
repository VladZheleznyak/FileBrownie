from dataclasses import replace

from filebrownie.ingestion.consistency import compare_inventories
from filebrownie.ingestion.discovery import discover_sources


def test_inventory_comparison_ignores_timestamp(tmp_path):
    (tmp_path / "report.pdf").write_bytes(b"synthetic")
    assert compare_inventories(discover_sources(tmp_path), discover_sources(tmp_path)).consistent


def test_inventory_comparison_reports_add_remove_and_change(tmp_path):
    (tmp_path / "removed.pdf").write_bytes(b"synthetic removed")
    (tmp_path / "changed.jpeg").write_bytes(b"synthetic before")
    before = discover_sources(tmp_path)
    (tmp_path / "removed.pdf").unlink()
    (tmp_path / "changed.jpeg").write_bytes(b"synthetic after")
    (tmp_path / "added.zip").write_bytes(b"synthetic unsupported")
    result = compare_inventories(before, discover_sources(tmp_path))
    assert result.added == ("added.zip",)
    assert result.removed == ("removed.pdf",)
    assert result.changed == ("changed.jpeg",)
    assert not result.consistent


def test_inventory_ignores_metadata_touch_warnings(tmp_path):
    (tmp_path / "report.pdf").write_bytes(b"synthetic")
    before = discover_sources(tmp_path)
    (record,) = before.records
    touched = replace(
        record,
        warnings=("METADATA_TOUCHED", "fields: mtime_ns 1->2"),
    )
    after = replace(before, records=(touched,))
    assert compare_inventories(before, after).consistent


def test_matching_failures_cannot_establish_source_consistency(tmp_path):
    (tmp_path / "broken.pdf").symlink_to(tmp_path / "absent")
    before = discover_sources(tmp_path)
    result = compare_inventories(before, replace(before))
    assert result.changed == ()
    assert result.unverifiable == ("broken.pdf",)
    assert not result.consistent
