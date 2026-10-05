from contextlib import contextmanager

import pymupdf
import pytest
from PIL import Image
from support import FakeVision

from filebrownie.evidence.models import TextSpan
from filebrownie.ingestion import scan
from filebrownie.ingestion.discovery import discover_sources
from filebrownie.presentation import cli
from filebrownie.storage.database import DatabaseError

pytestmark = pytest.mark.integration


@pytest.fixture
def scan_folders(tmp_path, repository):
    source, data = tmp_path / "sources", tmp_path / "data"
    source.mkdir()
    data.mkdir()
    repository.migrate()
    return source, data


def pdf(path, label="Synthetic laboratory evidence"):
    with pymupdf.open() as document:
        page = document.new_page(width=300, height=200)
        page.insert_text((20, 40), label, fontsize=12)
        document.save(path)


def test_scan_links_duplicate_sources_and_reuses_pdf_and_jpeg_cache(
    repository, scan_folders, monkeypatch
):
    source, data = scan_folders
    pdf(source / "a.pdf")
    (source / "copy.PDF").write_bytes((source / "a.pdf").read_bytes())
    Image.new("RGB", (40, 40), "white").save(source / "photo.jpg")
    (source / "unsupported.zip").write_bytes(b"synthetic archive")
    first = scan.scan_sources(repository, source, data)
    first_reads = repository.generation_reads(first)
    assert len(first_reads) == 2
    assert sorted(item.sources for item in first_reads) == [["a.pdf", "copy.PDF"], ["photo.jpg"]]
    assert all(not item.cache_hit for item in first_reads)
    assert {item.status for item in first_reads} == {"completed", "partial"}
    assert repository.generations()[0].state == "staged"

    def unexpected(*args):
        pytest.fail("Unchanged source was parsed again")

    monkeypatch.setattr(scan, "read_document", unexpected)
    second = scan.scan_sources(repository, source, data)
    assert all(item.cache_hit for item in repository.generation_reads(second))
    assert {item.reference for item in repository.generation_reads(second)} == {
        item.reference for item in first_reads
    }
    assert repository.load_inventory(second).unsupported_file_count == 1


@pytest.mark.parametrize("artifact", ["result", "raster", "missing"])
def test_corrupt_or_missing_cache_artifacts_trigger_new_read(repository, scan_folders, artifact):
    source, data = scan_folders
    pdf(source / "a.pdf")
    first = scan.scan_sources(repository, source, data)
    (prior,) = repository.generation_reads(first)
    directory = data / "evidence" / str(prior.reference)
    if artifact == "result":
        (directory / "result.json").write_text("{}")
    elif artifact == "raster":
        (directory / "pages" / "000001.png").write_bytes(b"synthetic corrupt raster")
    else:
        (directory / "pages" / "000001.png").unlink()
    second = scan.scan_sources(repository, source, data)
    (current,) = repository.generation_reads(second)
    assert not current.cache_hit
    assert current.reference != prior.reference
    assert current.status == "completed"


def test_reader_version_change_and_changed_source_do_not_reuse_stale_cache(
    repository, scan_folders, monkeypatch
):
    source, data = scan_folders
    pdf(source / "a.pdf")
    first = scan.scan_sources(repository, source, data)
    monkeypatch.setattr(scan, "reader_fingerprint", lambda: "f" * 64)
    second = scan.scan_sources(repository, source, data)
    assert not repository.generation_reads(second)[0].cache_hit
    pdf(source / "a.pdf", "Synthetic changed evidence")
    third = scan.scan_sources(repository, source, data)
    assert not repository.generation_reads(third)[0].cache_hit
    assert repository.generation_reads(third)[0].content_hash != (
        repository.generation_reads(first)[0].content_hash
    )


def test_interrupt_preserves_completed_cache_for_next_generation(
    repository, scan_folders, monkeypatch
):
    source, data = scan_folders
    pdf(source / "a.pdf")
    pdf(source / "b.pdf", "Synthetic second document")
    original = scan.read_document

    def interrupted(root, record, output):
        if record.relative_path == "b.pdf":
            raise KeyboardInterrupt
        return original(root, record, output)

    monkeypatch.setattr(scan, "read_document", interrupted)
    with pytest.raises(KeyboardInterrupt):
        scan.scan_sources(repository, source, data)
    (generation,) = repository.generations()
    assert generation.state == "interrupted"
    assert len(repository.generation_reads(generation.id)) == 1
    monkeypatch.setattr(scan, "read_document", original)
    second = scan.scan_sources(repository, source, data)
    reads = repository.generation_reads(second)
    assert len(reads) == 2
    assert {item.sources[0]: item.cache_hit for item in reads} == {"a.pdf": True, "b.pdf": False}


@pytest.mark.parametrize("change", ["modify", "add", "remove"])
def test_source_changes_during_scan_invalidate_generation(
    repository, scan_folders, monkeypatch, change
):
    source, data = scan_folders
    pdf(source / "a.pdf")
    original = scan.read_document

    def change_after_read(root, record, output):
        result = original(root, record, output)
        if change == "modify":
            pdf(source / "a.pdf", "Synthetic changed evidence")
        elif change == "add":
            (source / "added.zip").write_bytes(b"synthetic archive")
        else:
            (source / "a.pdf").unlink()
        return result

    monkeypatch.setattr(scan, "read_document", change_after_read)
    identifier = scan.scan_sources(repository, source, data)
    (generation,) = repository.generations()
    assert generation.id == identifier
    assert generation.state == "invalid"
    assert generation.reason == "SOURCE_CHANGED"
    assert generation.finished_at is not None
    assert repository.generation_reads(identifier)[0].reference is not None


def test_failed_documents_are_reported_and_retried_without_cache(
    repository, scan_folders, monkeypatch
):
    source, data = scan_folders
    (source / "broken.pdf").write_bytes(b"synthetic malformed")
    original = scan.read_document
    calls = 0

    def counted(*args):
        nonlocal calls
        calls += 1
        return original(*args)

    monkeypatch.setattr(scan, "read_document", counted)
    first = scan.scan_sources(repository, source, data)
    second = scan.scan_sources(repository, source, data)
    assert calls == 2
    for identifier in (first, second):
        (record,) = repository.generation_reads(identifier)
        assert record.status == "failed"
        assert record.page_count is None
        assert not record.cache_hit


def test_reader_scan_cannot_finish_with_missing_content_outcome(repository, scan_folders):
    source, _ = scan_folders
    pdf(source / "a.pdf")
    inventory = discover_sources(source)
    identifier = repository.begin_inventory(kind="reader")
    repository.save_inventory(identifier, inventory, finalize=False)
    with pytest.raises(DatabaseError, match="^GENERATION_READERS_INCOMPLETE$"):
        repository.finish_scan(identifier, inventory)
    assert repository.generations()[0].state == "running"


def test_reader_scan_cli_and_generation_inspection(repository, scan_folders, monkeypatch, capsys):
    source, data = scan_folders
    pdf(source / "a.pdf")
    monkeypatch.setenv("FILEBROWNIE_SOURCE_DIR", str(source))
    monkeypatch.setenv("FILEBROWNIE_DATA_DIR", str(data))

    @contextmanager
    def local_repository():
        yield repository

    monkeypatch.setattr(cli, "open_repository", local_repository)
    assert cli.main(["scan", "--readers-only"]) == 0
    identifier = repository.generations()[0].id
    assert cli.main(["evidence", "generation", str(identifier)]) == 0
    assert cli.main(["scan", "--readers-only"]) == 0
    output = capsys.readouterr().out
    assert "Discovering sources." in output
    assert "Supported files: 1; unsupported files: 0; unique contents to process: 1." in output
    assert "[1/1] reading: a.pdf (pdf)." in output
    assert "[1/1] cached reader: a.pdf (pdf, 1 page)." in output
    assert "Rechecking sources." in output
    assert "Synthetic laboratory evidence" not in output
    assert "state: staged" in output
    assert "cached: yes" in output
    assert "no OCR, vision, or medical interpretation" in output


class _QuietOcr:
    version = "test-ocr"

    def __init__(self, _models):
        pass

    def read(self, _image):
        return (TextSpan("ZEBRA-PROGRESS-9912", (1, 1, 8, 8), "ocr"),)


def test_full_scan_cli_prints_page_progress_without_document_text(
    repository, scan_folders, monkeypatch, capsys
):
    source, data = scan_folders
    pdf(source / "labs.pdf")
    Image.new("RGB", (40, 40), "white").save(source / "photo.jpg")
    (source / "notes.txt").write_bytes(b"not a medical file")
    monkeypatch.setenv("FILEBROWNIE_SOURCE_DIR", str(source))
    monkeypatch.setenv("FILEBROWNIE_DATA_DIR", str(data))
    monkeypatch.setenv("FILEBROWNIE_MODEL_DIR", str(scan_folders[1]))

    @contextmanager
    def local_repository():
        yield repository

    monkeypatch.setattr(cli, "open_repository", local_repository)
    monkeypatch.setattr(cli, "TesseractOcr", _QuietOcr)
    monkeypatch.setattr(cli, "LlamaVisionClient", lambda _url, _models: FakeVision({}))
    assert cli.main(["scan"]) == 0
    first = capsys.readouterr().out
    assert "Checking that this scan cannot reach the internet." in first
    assert "Supported files: 2; unsupported files: 1; unique contents to process: 2." in first
    assert "[1/2] reading: labs.pdf (pdf)." in first
    assert "page 1/1: text layer" in first
    assert "page 1/1: vision" in first
    assert "page 1/1: vision (cached)" not in first
    assert "ETA " in first
    assert "jpeg ~" in first
    assert "[2/2] reading: photo.jpg (jpeg)." in first
    assert "page 1/1: OCR" in first
    assert "page 1/1: OCR (cached)" not in first
    assert "Recording terminology proposals." in first
    assert "Rechecking sources." in first
    assert "Checking activation." in first
    assert "ZEBRA-PROGRESS-9912" not in first
    assert "Synthetic laboratory evidence" not in first

    assert cli.main(["scan"]) == 0
    second = capsys.readouterr().out
    assert "[1/2] cached reader: labs.pdf (pdf, 1 page)." in second
    assert "page 1/1: text layer" in second
    assert "page 1/1: vision (cached)" in second
    assert "page 1/1: OCR (cached)" in second
    assert "ETA " in second
    assert "ZEBRA-PROGRESS-9912" not in second
