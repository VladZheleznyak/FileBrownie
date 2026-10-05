import json
from pathlib import Path
from subprocess import TimeoutExpired

import pymupdf
import pytest
from PIL import Image

from filebrownie.evidence import readers, worker
from filebrownie.evidence.models import DocumentEvidence, ProcessingStatus, TextSpan, UnitEvidence
from filebrownie.evidence.readers import ReaderError, inspect_evidence, read_document
from filebrownie.ingestion.discovery import discover_sources

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


@pytest.fixture
def folders(tmp_path):
    source, data = tmp_path / "sources", tmp_path / "data"
    source.mkdir()
    data.mkdir()
    return source, data


def make_pdf(path, texts, rotation=0):
    with pymupdf.open() as document:
        for text in texts:
            page = document.new_page(width=300, height=200)
            page.insert_font(fontname="synthetic", fontfile=FONT)
            for index, line in enumerate(text.splitlines()):
                page.insert_text((20, 30 + index * 25), line, fontname="synthetic", fontsize=12)
            page.set_rotation(rotation)
        document.save(path)


def read_source(folders, name):
    source, data = folders
    record = next(item for item in discover_sources(source).records if item.relative_path == name)
    return read_document(source, record, data)


def test_pdf_multilingual_text_and_locations_roundtrip(folders):
    source, data = folders
    text = "Synthetic hemoglobin 125 g/L\nГемоглобин 125 г/л\nГемоглобін 125 г/л"
    make_pdf(source / "report.pdf", [text])
    original = (source / "report.pdf").read_bytes()
    result = read_source(folders, "report.pdf")
    assert result.evidence.status == ProcessingStatus.COMPLETED
    assert result.evidence.page_count == 1
    (unit,) = result.evidence.units
    assert "\n".join(span.text for span in unit.spans) == text
    assert unit.raster is not None
    assert (data / "evidence" / result.reference / unit.raster).exists()
    assert "TEXT_LAYER_COVERAGE_UNVERIFIED" in unit.warnings
    for span in unit.spans:
        x0, y0, x1, y1 = span.bbox
        assert 0 <= x0 < x1 <= unit.width
        assert 0 <= y0 < y1 <= unit.height
    assert inspect_evidence(data, result.reference) == result
    assert "pymupdf-" in result.evidence.reader_version
    assert (source / "report.pdf").read_bytes() == original
    assert not (data / "evidence" / result.reference / "input").exists()


def test_rotated_pdf_locations_match_rendered_image(folders):
    source, _ = folders
    make_pdf(source / "rotated.pdf", ["Synthetic rotated text"], rotation=90)
    (unit,) = read_source(folders, "rotated.pdf").evidence.units
    assert unit.status == ProcessingStatus.COMPLETED
    assert unit.width < unit.height
    assert all(0 <= span.bbox[0] < span.bbox[2] <= unit.width for span in unit.spans)
    assert all(0 <= span.bbox[1] < span.bbox[3] <= unit.height for span in unit.spans)


def test_blank_pdf_page_keeps_successful_page_and_requires_ocr(folders):
    source, _ = folders
    make_pdf(source / "mixed.pdf", ["Synthetic selectable text", ""])
    result = read_source(folders, "mixed.pdf").evidence
    assert result.status == ProcessingStatus.PARTIAL
    assert result.page_count == 2
    assert result.units[0].spans
    assert "OCR_REQUIRED" in result.units[1].warnings
    assert result.units[1].raster is not None


def test_oversized_jpeg_is_downscaled_instead_of_rejected(folders):
    source, data = folders
    Image.new("RGB", (4000, 3000), "white").save(source / "large.jpg", quality=85)
    result = read_source(folders, "large.jpg")
    (unit,) = result.evidence.units
    assert result.evidence.status == ProcessingStatus.PARTIAL
    assert unit.width * unit.height <= worker.MAX_PAGE_PIXELS
    assert unit.raster is not None
    assert (data / "evidence" / result.reference / unit.raster).exists()


def test_jpeg_orientation_is_normalized_without_claiming_text(folders):
    source, data = folders
    exif = Image.Exif()
    exif[274] = 6
    Image.new("RGB", (120, 80), "white").save(source / "photo.JPEG", exif=exif)
    result = read_source(folders, "photo.JPEG")
    (unit,) = result.evidence.units
    assert result.evidence.status == ProcessingStatus.PARTIAL
    assert unit.spans == ()
    assert (unit.width, unit.height) == (80, 120)
    assert unit.warnings == ("OCR_REQUIRED", "IMAGE_ORIENTATION_APPLIED")
    with Image.open(data / "evidence" / result.reference / unit.raster) as image:
        assert image.size == (80, 120)


@pytest.mark.parametrize("name", ["broken.pdf", "broken.jpg"])
def test_corrupt_file_preserves_unknown_page_count_and_has_no_raw_diagnostics(
    folders, name, capsys
):
    source, _ = folders
    (source / name).write_bytes(b"synthetic malformed input")
    result = read_source(folders, name).evidence
    assert result.status == ProcessingStatus.FAILED
    assert result.page_count is None
    assert result.warnings == ("DOCUMENT_READ_FAILED",)
    output = capsys.readouterr()
    assert output.out == output.err == ""


def test_password_protected_pdf_is_explicitly_unsupported(folders):
    source, _ = folders
    with pymupdf.open() as document:
        document.new_page()
        document.save(
            source / "encrypted.pdf",
            encryption=pymupdf.PDF_ENCRYPT_AES_256,
            owner_pw="synthetic-owner",
            user_pw="synthetic-user",
        )
    result = read_source(folders, "encrypted.pdf").evidence
    assert result.status == ProcessingStatus.UNSUPPORTED
    assert result.warnings == ("PASSWORD_PROTECTED",)
    assert result.page_count is None


def test_png_disguised_as_jpeg_is_not_accepted(folders):
    source, _ = folders
    Image.new("RGB", (40, 40), "white").save(source / "disguised.jpg", format="PNG")
    result = read_source(folders, "disguised.jpg").evidence
    assert result.status == ProcessingStatus.FAILED
    assert result.warnings == ("JPEG_FORMAT_MISMATCH",)


def test_source_changed_since_inventory_is_not_read(folders):
    source, data = folders
    (source / "report.pdf").write_bytes(b"synthetic before")
    (record,) = discover_sources(source).records
    (source / "report.pdf").write_bytes(b"synthetic after")
    with pytest.raises(ReaderError, match="^SOURCE_CHANGED$"):
        read_document(source, record, data)
    assert not list(data.rglob("input"))


def test_symlink_replacement_cannot_redirect_source_copy(folders, tmp_path):
    source, data = folders
    path = source / "report.pdf"
    path.write_bytes(b"synthetic before")
    (record,) = discover_sources(source).records
    external = tmp_path / "external.pdf"
    external.write_bytes(b"synthetic external")
    path.unlink()
    path.symlink_to(external)
    with pytest.raises(ReaderError, match="^READER_STORAGE_OR_SOURCE_UNAVAILABLE$"):
        read_document(source, record, data)
    assert not list(data.rglob("input"))


def test_unsupported_file_is_never_sent_to_worker(folders, monkeypatch):
    source, data = folders
    (source / "archive.zip").write_bytes(b"synthetic archive")
    (record,) = discover_sources(source).records

    def unexpected(*args, **kwargs):
        pytest.fail("An unsupported file reached the parser")

    monkeypatch.setattr(readers.subprocess, "run", unexpected)
    with pytest.raises(ReaderError, match="^SOURCE_NOT_READABLE_OR_UNSUPPORTED$"):
        read_document(source, record, data)
    assert not (data / "evidence").exists()


def test_input_size_limit_prevents_parser_call(folders, monkeypatch):
    source, data = folders
    (source / "large.pdf").write_bytes(b"synthetic longer than limit")
    (record,) = discover_sources(source).records
    monkeypatch.setattr(readers, "MAX_INPUT_BYTES", 2)
    with pytest.raises(ReaderError, match="^INPUT_SIZE_LIMIT$"):
        read_document(source, record, data)
    assert not list(data.rglob("input"))


def test_timed_out_worker_retains_completed_page(folders, monkeypatch):
    source, data = folders
    (source / "report.pdf").write_bytes(b"synthetic")
    (record,) = discover_sources(source).records
    completed = UnitEvidence(
        1, ProcessingStatus.COMPLETED, (), (TextSpan("Synthetic evidence", (1, 1, 20, 20)),)
    )

    def timeout(command, **kwargs):
        directory = Path(command[-2])
        result = DocumentEvidence(ProcessingStatus.PARTIAL, (), 2, (completed,))
        directory.joinpath("manifest.json").write_text(json.dumps(result.to_dict()))
        assert kwargs["stdout"] == kwargs["stderr"] == readers.subprocess.DEVNULL
        raise TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(readers.subprocess, "run", timeout)
    result = read_document(source, record, data).evidence
    assert result.status == ProcessingStatus.PARTIAL
    assert result.units[0] == completed
    assert result.units[1].number == 2
    assert result.units[1].warnings == ("READER_TIMEOUT",)


def test_failed_pdf_page_does_not_discard_other_pages(tmp_path, monkeypatch):
    directory = tmp_path / "job"
    directory.mkdir()
    directory.joinpath("pages").mkdir()
    make_pdf(directory / "input", ["Synthetic first page", "Synthetic second page"])
    original = worker._pdf_page

    def fail_second(document, number, directory):
        if number == 2:
            raise RuntimeError("synthetic-sensitive-parser-error")
        return original(document, number, directory)

    monkeypatch.setattr(worker, "_pdf_page", fail_second)
    worker._read_pdf(directory)
    result = readers._load_result(directory)
    assert result.status == ProcessingStatus.PARTIAL
    assert result.units[0].spans
    assert result.units[1].status == ProcessingStatus.FAILED
    assert result.units[1].warnings == ("PDF_PAGE_READ_FAILED",)
    assert "synthetic-sensitive" not in directory.joinpath("manifest.json").read_text()


def test_pdf_page_limit_reports_unread_coverage(tmp_path, monkeypatch):
    directory = tmp_path / "job"
    directory.mkdir()
    directory.joinpath("pages").mkdir()
    make_pdf(directory / "input", ["Synthetic first page", "Synthetic second page"])
    monkeypatch.setattr(worker, "MAX_PAGES", 1)
    worker._read_pdf(directory)
    result = readers._load_result(directory)
    assert result.status == ProcessingStatus.PARTIAL
    assert result.page_count == 2
    assert len(result.units) == 1
    assert result.warnings == ("DOCUMENT_PAGE_LIMIT",)


def test_pdf_continues_at_lower_dpi_when_pixel_budget_is_tight(tmp_path, monkeypatch):
    directory = tmp_path / "job"
    directory.mkdir()
    directory.joinpath("pages").mkdir()
    make_pdf(directory / "input", ["Synthetic page one", "Synthetic page two"])
    monkeypatch.setattr(worker, "MAX_TOTAL_PIXELS", 500_000)
    worker._read_pdf(directory)
    result = readers._load_result(directory)
    assert result.status == ProcessingStatus.COMPLETED
    assert len(result.units) == 2
    assert all(unit.status != ProcessingStatus.SKIPPED for unit in result.units)


def test_pdf_pixel_limit_is_a_failed_unit(tmp_path, monkeypatch):
    directory = tmp_path / "job"
    directory.mkdir()
    directory.joinpath("pages").mkdir()
    make_pdf(directory / "input", ["Synthetic large page"])
    monkeypatch.setattr(worker, "MAX_PAGE_PIXELS", 10)
    worker._read_pdf(directory)
    (unit,) = readers._load_result(directory).units
    assert unit.status == ProcessingStatus.FAILED
    assert unit.warnings == ("PAGE_PIXEL_LIMIT",)
    assert unit.raster is None


def test_tiny_text_is_retained_with_quality_warning(folders):
    source, _ = folders
    make_pdf(source / "short.pdf", ["Iron 4"])
    (unit,) = read_source(folders, "short.pdf").evidence.units
    assert unit.status == ProcessingStatus.PARTIAL
    assert unit.spans[0].text == "Iron 4"
    assert "TEXT_LAYER_LOW_QUALITY" in unit.warnings
