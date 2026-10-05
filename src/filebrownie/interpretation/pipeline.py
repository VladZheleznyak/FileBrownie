"""Per-content processing: located text (PDF layer or OCR), vision extraction, interpretation.

OCR and vision outputs are memoized in the step cache by exact raster bytes plus the step's
model/prompt/configuration version, independently of generations (D9).
"""

import hashlib
from collections.abc import Callable, Sequence
from pathlib import Path

from filebrownie.evidence.models import (
    DocumentEvidence,
    ProcessingStatus,
    TextSpan,
    UnitEvidence,
)
from filebrownie.evidence.ocr import OcrEngine, OcrError
from filebrownie.evidence.worker import MAX_ARTIFACT_BYTES
from filebrownie.ingestion.progress import ProgressFn, ScanProgress
from filebrownie.interpretation.extract import interpret_page
from filebrownie.interpretation.grounding import PageText, vision_lab_row_hints
from filebrownie.interpretation.llama_vision import DROPPED_LAB_ROWS_HINT
from filebrownie.interpretation.vision import (
    VisionClient,
    VisionError,
    merge_vision_lab_rows,
    parse_page,
)
from filebrownie.storage.facts import UnitRecord, step_cache_key

_NEEDS_OCR = {"OCR_REQUIRED", "TEXT_LAYER_LOW_QUALITY"}
_UNRESOLVED_READER_PARTIAL = {"TEXT_LOCATION_UNAVAILABLE"}


def _ocr_payload(ocr: OcrEngine, image: bytes) -> dict:
    try:
        return {"spans": [_span_json(span) for span in ocr.read(image)]}
    except OcrError:
        raise
    except Exception:
        raise OcrError("OCR_FAILED") from None  # raw engine diagnostics never escape


def _span_json(span: TextSpan) -> dict:
    return {
        "text": span.text,
        "bbox": list(span.bbox) if span.bbox else None,
        "reader": span.reader,
    }


def _span_from_json(item: dict) -> TextSpan:
    bbox = tuple(item["bbox"]) if item["bbox"] else None
    return TextSpan(item["text"], bbox, item["reader"])


def _run_cached(
    repository,
    step,
    content_hash,
    format,
    number,
    raster_hash,
    version,
    compute,
    validate,
    on_begin: Callable[[bool], None] | None = None,
):
    key = step_cache_key(step, content_hash, format, number, raster_hash, version)
    payload = repository.facts.cached_step(key)
    if payload is not None:
        try:
            validate(payload)
        except Exception:  # noqa: BLE001 - a damaged entry is recomputed and replaced
            payload = None
        else:
            if on_begin is not None:
                on_begin(True)
            return payload
    if on_begin is not None:
        on_begin(False)
    payload = compute()
    repository.facts.cache_step(key, step, content_hash, format, number, version, payload)
    return payload


def _step(
    progress: ProgressFn | None, pages: int | None, unit: int, step: str, cached: bool = False
) -> None:
    if progress is not None:
        progress(ScanProgress(stage="step", unit=unit, pages=pages, step=step, cached=cached))


def _merge_dropped_lab_retry(
    repository,
    vision: VisionClient,
    image: bytes,
    content_hash: str,
    format: str,
    unit_number: int,
    raster_hash: str,
    page,
    progress: ProgressFn | None,
    page_count: int | None,
):
    retry_version = f"{vision.version};dropped-retry=1"

    def retry() -> dict:
        try:
            claimed = vision.extract(image, lab_row_hints=(DROPPED_LAB_ROWS_HINT,))
            parse_page(
                {
                    "document_class": page.document_class,
                    "handwriting": page.handwriting,
                    "context_missing": page.context_missing,
                    "dates": [],
                    "lab_rows": claimed.get("lab_rows", []),
                    "events": [],
                }
            )
        except VisionError:
            raise
        except Exception:
            raise VisionError("VISION_FAILED") from None
        return {"page": claimed}

    try:
        extra_payload = _run_cached(
            repository,
            "vision-dropped-retry",
            content_hash,
            format,
            unit_number,
            raster_hash,
            retry_version,
            retry,
            lambda cached: cached["page"],
            lambda hit: _step(progress, page_count, unit_number, "vision-dropped-retry", hit),
        )
    except VisionError:
        return page
    extra = parse_page(
        {
            "document_class": page.document_class,
            "handwriting": False,
            "context_missing": False,
            "dates": [],
            "lab_rows": extra_payload["page"].get("lab_rows", []),
            "events": [],
        }
    )
    return merge_vision_lab_rows(page, extra)


def _process_unit(
    repository,
    data: Path,
    reference: str,
    content_hash: str,
    format: str,
    unit: UnitEvidence,
    ocr: OcrEngine | None,
    vision: VisionClient | None,
    progress: ProgressFn | None = None,
    page_count: int | None = None,
    source_paths: tuple[str, ...] = (),
) -> UnitRecord:
    warnings = list(unit.warnings)
    if unit.status in (ProcessingStatus.FAILED, ProcessingStatus.SKIPPED) or unit.raster is None:
        _step(progress, page_count, unit.number, "unavailable")
        return UnitRecord(unit.number, unit.status.value, tuple(warnings), "none")
    raster = data / "evidence" / reference / unit.raster
    if raster.is_symlink() or not raster.is_file() or raster.stat().st_size > MAX_ARTIFACT_BYTES:
        _step(progress, page_count, unit.number, "unavailable")
        return UnitRecord(unit.number, "failed", (*warnings, "RASTER_UNAVAILABLE"), "none")
    image = raster.read_bytes()
    raster_hash = hashlib.sha256(image).hexdigest()

    spans: tuple[TextSpan, ...] = unit.spans
    source = "pdf-text"
    resolved_ocr = False
    failures: list[str] = []
    if _NEEDS_OCR & set(warnings):
        spans, source = (), "none"
        if ocr is None:
            failures.append("OCR_UNAVAILABLE")
            _step(progress, page_count, unit.number, "ocr-unavailable")
        else:
            try:
                payload = _run_cached(
                    repository,
                    "ocr",
                    content_hash,
                    format,
                    unit.number,
                    raster_hash,
                    ocr.version,
                    lambda: _ocr_payload(ocr, image),
                    lambda cached: [_span_from_json(item) for item in cached["spans"]],
                    lambda hit: _step(progress, page_count, unit.number, "ocr", hit),
                )
            except OcrError as error:
                failures.append(str(error) if str(error).startswith("OCR_") else "OCR_FAILED")
            else:
                spans = tuple(_span_from_json(item) for item in payload["spans"])
                source, resolved_ocr = "ocr", True
                warnings = [code for code in warnings if code not in _NEEDS_OCR]
    elif spans:
        _step(progress, page_count, unit.number, "text")

    interpretation = None
    if vision is None:
        failures.append("VISION_UNAVAILABLE")
        _step(progress, page_count, unit.number, "vision-unavailable")
    else:

        def extract() -> dict:
            try:
                claimed = vision.extract(image)
                parse_page(claimed)  # only validated output is ever cached
            except VisionError:
                raise
            except Exception:
                raise VisionError("VISION_FAILED") from None
            return {"page": claimed}

        try:
            payload = _run_cached(
                repository,
                "vision",
                content_hash,
                format,
                unit.number,
                raster_hash,
                vision.version,
                extract,
                lambda cached: parse_page(cached["page"]),
                lambda hit: _step(progress, page_count, unit.number, "vision", hit),
            )
            page = parse_page(payload["page"])
            if page.dropped > 0 and page.document_class == "lab_report":
                page = _merge_dropped_lab_retry(
                    repository,
                    vision,
                    image,
                    content_hash,
                    format,
                    unit.number,
                    raster_hash,
                    page,
                    progress,
                    page_count,
                )
        except VisionError as error:
            failures.append(str(error) if str(error).startswith("VISION_") else "VISION_FAILED")
        else:
            interpretation = interpret_page(spans, page, source_paths)
            page_text = PageText.build(spans)
            covered = {
                index
                for fact in interpretation.lab_facts
                for index in (*fact.evidence, *fact.alternative_evidence)
            }
            hints = vision_lab_row_hints(page_text, covered)
            if hints:
                supplement_version = f"{vision.version};supplement={hashlib.sha256('|'.join(hints).encode()).hexdigest()[:8]}"

                def supplement() -> dict:
                    try:
                        claimed = vision.extract(image, lab_row_hints=hints)
                        parse_page(
                            {
                                "document_class": page.document_class,
                                "handwriting": page.handwriting,
                                "context_missing": page.context_missing,
                                "dates": [],
                                "lab_rows": claimed.get("lab_rows", []),
                                "events": [],
                            }
                        )
                    except VisionError:
                        raise
                    except Exception:
                        raise VisionError("VISION_FAILED") from None
                    return {"page": claimed}

                try:
                    extra_payload = _run_cached(
                        repository,
                        "vision-supplement",
                        content_hash,
                        format,
                        unit.number,
                        raster_hash,
                        supplement_version,
                        supplement,
                        lambda cached: cached["page"],
                        lambda hit: _step(
                            progress, page_count, unit.number, "vision-supplement", hit
                        ),
                    )
                    extra = parse_page(
                        {
                            "document_class": page.document_class,
                            "handwriting": False,
                            "context_missing": False,
                            "dates": [],
                            "lab_rows": extra_payload["page"].get("lab_rows", []),
                            "events": [],
                        }
                    )
                    page = merge_vision_lab_rows(page, extra)
                    interpretation = interpret_page(spans, page, source_paths)
                except VisionError:
                    pass
    if interpretation is not None:
        warnings.extend(item.code for item in interpretation.warnings)
    warnings.extend(failures)

    ready = (
        not failures
        and not (_NEEDS_OCR & set(warnings))
        and not (_UNRESOLVED_READER_PARTIAL & set(warnings))
        and (unit.status == ProcessingStatus.COMPLETED or resolved_ocr)
    )
    return UnitRecord(
        unit.number,
        "completed" if ready else "partial",
        tuple(dict.fromkeys(warnings)),
        source,
        spans,
        interpretation,
    )


def process_content(
    repository,
    data: Path,
    reference: str,
    content_hash: str,
    format: str,
    evidence: DocumentEvidence,
    ocr: OcrEngine | None,
    vision: VisionClient | None,
    progress: ProgressFn | None = None,
    source_paths: tuple[str, ...] = (),
) -> tuple[str, Sequence[str], tuple[UnitRecord, ...]]:
    """Return the file status, file-level warnings, and per-unit records for one content."""
    units = tuple(
        _process_unit(
            repository,
            data,
            reference,
            content_hash,
            format,
            unit,
            ocr,
            vision,
            progress,
            evidence.page_count,
            source_paths,
        )
        for unit in evidence.units
    )
    complete = (
        evidence.page_count is not None
        and len(units) == evidence.page_count
        and all(unit.status == "completed" for unit in units)
        and evidence.status != ProcessingStatus.FAILED
    )
    if complete:
        status = "completed"
    elif any(unit.status in ("completed", "partial") for unit in units):
        status = "partial"
    elif not units:
        status = evidence.status.value
    else:
        status = "failed"
    return status, evidence.warnings, units
