import hashlib
import os
import stat

import pytest

from filebrownie.ingestion import discovery
from filebrownie.ingestion.discovery import DiscoveryStatus, InventoryError, discover_sources


def test_inventory_recurses_and_preserves_identical_content_paths(tmp_path):
    (tmp_path / "nested").mkdir()
    originals = {
        "report.PDF": b"synthetic identical bytes",
        "nested/copy.jpeg": b"synthetic identical bytes",
        "nested/photo.JPG": b"synthetic other bytes",
        "archive.zip": b"synthetic archive; never expand",
        "notes.txt": b"synthetic notes; never parse",
    }
    for name, content in originals.items():
        (tmp_path / name).write_bytes(content)
    result = discover_sources(tmp_path)
    assert result.supported_file_count == 3
    assert result.unsupported_file_count == 2
    assert [record.relative_path for record in result.records] == sorted(originals)
    digest = hashlib.sha256(originals["report.PDF"]).hexdigest()
    assert result.content_sources()[digest] == ("nested/copy.jpeg", "report.PDF")
    assert {record.status for record in result.records} == {
        DiscoveryStatus.READY,
        DiscoveryStatus.UNSUPPORTED,
    }
    assert {name: (tmp_path / name).read_bytes() for name in originals} == originals
    assert sorted(
        path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*") if path.is_file()
    ) == sorted(originals)


def test_symlinks_and_special_files_are_not_read(tmp_path):
    sources = tmp_path / "sources"
    sources.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (external / "hidden.pdf").write_bytes(b"synthetic outside source")
    (sources / "linked.pdf").symlink_to(external / "hidden.pdf")
    (sources / "linked-directory").symlink_to(external, target_is_directory=True)
    (sources / "broken.jpg").symlink_to(tmp_path / "absent")
    os.mkfifo(sources / "pipe.pdf")
    result = discover_sources(sources)
    assert len(result.records) == 4
    assert all(record.status == DiscoveryStatus.SKIPPED for record in result.records)
    assert all(record.content_hash is None for record in result.records)
    assert result.supported_file_count == 0
    assert {record.warnings for record in result.records} == {
        ("SYMLINK_NOT_FOLLOWED",),
        ("SPECIAL_FILE_NOT_READ",),
    }


def test_missing_root_has_sanitized_error(tmp_path):
    with pytest.raises(InventoryError, match="^SOURCE_FOLDER_UNAVAILABLE$"):
        discover_sources(tmp_path / "synthetic-private-name")


def test_changed_file_has_no_usable_fingerprint(tmp_path, monkeypatch):
    source = tmp_path / "report.pdf"
    source.write_bytes(b"synthetic initial bytes")
    original_fstat = discovery.os.fstat
    calls = 0

    def changing_fstat(descriptor):
        nonlocal calls
        calls += 1
        # The root directory check is not needed; these are file fstat calls.
        if calls == 2:
            source.write_bytes(b"synthetic changed and longer bytes")
        return original_fstat(descriptor)

    monkeypatch.setattr(discovery.os, "fstat", changing_fstat)
    (record,) = discover_sources(tmp_path).records
    assert record.status == DiscoveryStatus.FAILED
    assert record.content_hash is None
    assert record.warnings[0] == "SOURCE_CHANGED"
    assert record.warnings[1].startswith("fields: size")


def test_unreadable_file_error_does_not_keep_raw_diagnostics(tmp_path, monkeypatch):
    (tmp_path / "report.pdf").write_bytes(b"synthetic")

    def denied(*args):
        raise PermissionError("synthetic-sensitive-diagnostic")

    monkeypatch.setattr(discovery, "_fingerprint", denied)
    (record,) = discover_sources(tmp_path).records
    assert record.status == DiscoveryStatus.FAILED
    assert record.warnings == ("SOURCE_UNREADABLE",)
    assert "synthetic-sensitive-diagnostic" not in repr(record)


def test_mtime_only_change_still_fingerprints(tmp_path, monkeypatch):
    source = tmp_path / "report.pdf"
    source.write_bytes(b"synthetic stable bytes")
    original_fstat = discovery.os.fstat

    def touch_mtime(descriptor):
        result = original_fstat(descriptor)
        if stat.S_ISREG(result.st_mode):
            return os.stat_result(
                (
                    result.st_mode,
                    result.st_ino,
                    result.st_dev,
                    result.st_nlink,
                    result.st_uid,
                    result.st_gid,
                    result.st_size,
                    result.st_atime,
                    result.st_mtime + 1,
                    result.st_ctime + 1,
                )
            )
        return result

    monkeypatch.setattr(discovery.os, "fstat", touch_mtime)
    (record,) = discover_sources(tmp_path).records
    assert record.status == DiscoveryStatus.READY
    assert record.content_hash is not None
    assert "METADATA_TOUCHED" in record.warnings
    assert "mtime_ns" in record.warnings[1]


def test_directory_depth_limit_is_visible(tmp_path, monkeypatch):
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "report.pdf").write_bytes(b"synthetic")
    monkeypatch.setattr(discovery, "MAX_DIRECTORY_DEPTH", 0)
    (record,) = discover_sources(tmp_path).records
    assert record.kind == "directory"
    assert record.warnings == ("DIRECTORY_DEPTH_LIMIT",)
