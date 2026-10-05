"""Internal parser worker. Diagnostics go to /dev/null; evidence goes to local files."""

import json
import math
import resource
import sys
from dataclasses import replace
from importlib.metadata import version
from pathlib import Path

from filebrownie.evidence.models import DocumentEvidence, ProcessingStatus, TextSpan, UnitEvidence

MAX_PAGES = 200
MAX_PAGE_PIXELS = 8_000_000
MAX_OPEN_PIXELS = 50_000_000
MAX_TOTAL_PIXELS = 100_000_000
MAX_ARTIFACT_BYTES = 256 * 1024 * 1024
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
DPI = 150
MIN_PDF_DPI = 72
_PDF_DPI_STEPS = (150, 100, 75, MIN_PDF_DPI)


def _fit_within_pixels(width: int, height: int, max_pixels: int) -> tuple[int, int]:
    if width * height <= max_pixels:
        return width, height
    scale = math.sqrt(max_pixels / (width * height))
    new_w = max(1, int(width * scale))
    new_h = max(1, int(height * scale))
    while new_w * new_h > max_pixels:
        if new_w >= new_h and new_w > 1:
            new_w -= 1
        elif new_h > 1:
            new_h -= 1
        else:
            break
    return new_w, new_h


def _save(directory: Path, result: DocumentEvidence) -> None:
    result = replace(
        result,
        reader_version=f"reader-1;pymupdf-{version('PyMuPDF')};pillow-{version('Pillow')};dpi-{DPI}",
    )
    temporary = directory / "manifest.tmp"
    payload = json.dumps(result.to_dict(), ensure_ascii=True)
    if len(payload.encode()) > MAX_MANIFEST_BYTES:
        raise ValueError("MANIFEST_SIZE_LIMIT")
    temporary.write_text(payload)
    temporary.replace(directory / "manifest.json")


def _summary(result: DocumentEvidence) -> DocumentEvidence:
    if result.page_count is None:
        return result
    complete = len(result.units) == result.page_count and all(
        unit.status == ProcessingStatus.COMPLETED for unit in result.units
    )
    retained = any(unit.raster is not None or unit.spans for unit in result.units)
    return replace(
        result,
        status=ProcessingStatus.COMPLETED
        if complete
        else (ProcessingStatus.PARTIAL if retained else ProcessingStatus.FAILED),
    )


def _quality_warning(spans: tuple[TextSpan, ...]) -> str | None:
    text = " ".join(span.text for span in spans)
    if not text.strip():
        return "OCR_REQUIRED"
    if (
        sum(character.isalnum() for character in text) < 10
        or (text.count("\ufffd") + text.count("\x00")) / max(1, len(text)) > 0.02
    ):
        return "TEXT_LAYER_LOW_QUALITY"
    return None


def _pdf_page(document, number: int, directory: Path, dpi: int = DPI) -> UnitEvidence:
    import pymupdf

    page = document.load_page(number - 1)
    scale = dpi / 72
    width, height = page.rect.width * scale, page.rect.height * scale
    if not all(math.isfinite(value) and value > 0 for value in (width, height)):
        return UnitEvidence(number, ProcessingStatus.FAILED, ("INVALID_PAGE_GEOMETRY",))
    if math.ceil(width) * math.ceil(height) > MAX_PAGE_PIXELS:
        return UnitEvidence(number, ProcessingStatus.FAILED, ("PAGE_PIXEL_LIMIT",))
    raster_name = f"pages/{number:06d}.png"
    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
    pixmap.save(directory / raster_name)
    spans = []
    warnings = ["TEXT_LAYER_COVERAGE_UNVERIFIED"]
    flags = pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES
    for block in page.get_text("dict", flags=flags)["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                if not span["text"]:
                    continue
                bbox = (
                    pymupdf.Rect(span["bbox"]) * page.rotation_matrix * pymupdf.Matrix(scale, scale)
                )
                coordinates = tuple(bbox)
                if not all(math.isfinite(value) for value in coordinates) or not (
                    0 <= bbox.x0 < bbox.x1 <= pixmap.width
                    and 0 <= bbox.y0 < bbox.y1 <= pixmap.height
                ):
                    warnings.append("TEXT_LOCATION_UNAVAILABLE")
                    spans.append(TextSpan(span["text"], None))
                    continue
                spans.append(TextSpan(span["text"], coordinates))
    spans = tuple(spans)
    quality = _quality_warning(spans)
    if quality:
        warnings.append(quality)
    return UnitEvidence(
        number,
        ProcessingStatus.PARTIAL
        if quality or "TEXT_LOCATION_UNAVAILABLE" in warnings
        else ProcessingStatus.COMPLETED,
        tuple(dict.fromkeys(warnings)),
        spans,
        raster_name,
        pixmap.width,
        pixmap.height,
    )


def _read_pdf(directory: Path) -> None:
    import pymupdf

    pymupdf.TOOLS.mupdf_display_errors(False)
    pymupdf.TOOLS.mupdf_display_warnings(False)
    with pymupdf.open(directory / "input", filetype="pdf") as document:
        if document.needs_pass:
            _save(
                directory,
                DocumentEvidence(ProcessingStatus.UNSUPPORTED, ("PASSWORD_PROTECTED",), None),
            )
            return
        page_count = document.page_count
        warnings = ("PDF_REPAIRED",) if document.is_repaired else ()
        result = DocumentEvidence(ProcessingStatus.FAILED, warnings, page_count)
        _save(directory, result)
        if not page_count:
            _save(directory, replace(result, warnings=(*warnings, "EMPTY_DOCUMENT")))
            return
        pixels = 0
        artifact_bytes = 0
        for number in range(1, min(page_count, MAX_PAGES) + 1):
            unit = None
            raster = None
            for dpi in _PDF_DPI_STEPS:
                directory.joinpath(f"pages/{number:06d}.png").unlink(missing_ok=True)
                try:
                    unit = _pdf_page(document, number, directory, dpi=dpi)
                except Exception:
                    unit = UnitEvidence(number, ProcessingStatus.FAILED, ("PDF_PAGE_READ_FAILED",))
                if unit.status == ProcessingStatus.FAILED and unit.warnings == ("PAGE_PIXEL_LIMIT",):
                    continue
                raster = directory / unit.raster if unit.raster else None
                page_pixels = (unit.width or 0) * (unit.height or 0)
                page_bytes = raster.stat().st_size if raster and raster.exists() else 0
                if (
                    pixels + page_pixels <= MAX_TOTAL_PIXELS
                    and artifact_bytes + page_bytes <= MAX_ARTIFACT_BYTES
                ):
                    break
                if raster:
                    raster.unlink(missing_ok=True)
            else:
                unit = UnitEvidence(number, ProcessingStatus.SKIPPED, ("DOCUMENT_RESOURCE_LIMIT",))
                result = replace(
                    result,
                    warnings=(*result.warnings, "DOCUMENT_RESOURCE_LIMIT"),
                    units=(*result.units, unit),
                )
                break
            pixels += (unit.width or 0) * (unit.height or 0)
            raster = directory / unit.raster if unit.raster else None
            artifact_bytes += raster.stat().st_size if raster and raster.exists() else 0
            result = replace(result, units=(*result.units, unit))
            _save(directory, _summary(result))
        if page_count > MAX_PAGES:
            result = replace(result, warnings=(*result.warnings, "DOCUMENT_PAGE_LIMIT"))
        _save(directory, _summary(result))


def _read_jpeg(directory: Path) -> None:
    from PIL import Image, ImageOps

    Image.MAX_IMAGE_PIXELS = MAX_OPEN_PIXELS
    try:
        opened = Image.open(directory / "input")
    except Image.DecompressionBombError:
        _save(directory, DocumentEvidence(ProcessingStatus.FAILED, ("IMAGE_PIXEL_LIMIT",), 1))
        return
    with opened as image:
        if image.format != "JPEG":
            _save(
                directory,
                DocumentEvidence(ProcessingStatus.FAILED, ("JPEG_FORMAT_MISMATCH",), None),
            )
            return
        if image.width * image.height > MAX_OPEN_PIXELS:
            _save(directory, DocumentEvidence(ProcessingStatus.FAILED, ("IMAGE_PIXEL_LIMIT",), 1))
            return
        image.load()
        orientation = image.getexif().get(274, 1)
        normalized = ImageOps.exif_transpose(image).convert("RGB")
        normalized.info.clear()
        target = _fit_within_pixels(normalized.width, normalized.height, MAX_PAGE_PIXELS)
        if target != normalized.size:
            normalized = normalized.resize(target, Image.Resampling.LANCZOS)
        normalized.save(directory / "pages/000001.png")
        warnings = ("OCR_REQUIRED",)
        if orientation != 1:
            warnings += ("IMAGE_ORIENTATION_APPLIED",)
        unit = UnitEvidence(
            1,
            ProcessingStatus.PARTIAL,
            warnings,
            (),
            "pages/000001.png",
            normalized.width,
            normalized.height,
        )
        _save(directory, DocumentEvidence(ProcessingStatus.PARTIAL, (), 1, (unit,)))


def main() -> int:
    directory, format = Path(sys.argv[1]), sys.argv[2]
    # Applied before importing native parsers. The parent also enforces wall time.
    resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_ARTIFACT_BYTES,) * 2)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    directory.joinpath("pages").mkdir()
    try:
        if format == "pdf":
            _read_pdf(directory)
        elif format == "jpeg":
            _read_jpeg(directory)
        else:
            return 2
    except Exception:
        # On a late file failure, retain an already-written manifest for the parent.
        if directory.joinpath("manifest.json").exists():
            return 1
        _save(directory, DocumentEvidence(ProcessingStatus.FAILED, ("DOCUMENT_READ_FAILED",), None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
