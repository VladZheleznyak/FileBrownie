"""Serialized scans: `reader` stops after readers; `scan` runs the full pipeline."""

import time
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from filebrownie.evidence.cache import (
    artifact_checksums,
    cache_eligible,
    cache_key,
    reader_fingerprint,
    verified_cache,
)
from filebrownie.evidence.models import DocumentEvidence, ProcessingStatus
from filebrownie.evidence.network import ensure_isolated_network, ensure_no_outbound
from filebrownie.evidence.ocr import OcrEngine
from filebrownie.evidence.readers import ReaderError, read_document
from filebrownie.ingestion.discovery import DiscoveryStatus, discover_sources
from filebrownie.ingestion.progress import ProgressFn, ScanProgress
from filebrownie.interpretation.pipeline import process_content
from filebrownie.interpretation.vision import VisionClient
from filebrownie.storage.database import ActivationResult, DatabaseError, Repository


def _report(progress: ProgressFn | None, **fields) -> None:
    if progress is not None:
        progress(ScanProgress(**fields))


def _work_items(inventory):
    ready = [
        record
        for record in inventory.records
        if record.status == DiscoveryStatus.READY and record.content_hash and record.format
    ]
    counts: dict[tuple[str, str], int] = {}
    for record in ready:
        identity = (record.content_hash, record.format.value)
        counts[identity] = counts.get(identity, 0) + 1
    seen: set[tuple[str, str]] = set()
    work = []
    for record in ready:
        identity = (record.content_hash, record.format.value)
        if identity in seen:
            continue
        seen.add(identity)
        work.append((record, identity, counts[identity] - 1))
    return work


def scan_sources(
    repository: Repository,
    source: Path,
    data: Path,
    kind: str = "reader",
    ocr: OcrEngine | None = None,
    vision: VisionClient | None = None,
    progress: ProgressFn | None = None,
) -> UUID:
    """The caller must hold the shared application lock throughout this operation."""
    ensure_isolated_network()
    if kind == "scan":
        _report(progress, stage="network")
        ensure_no_outbound()  # medical processing refuses to run with internet access (D13)
    repository.require_schema()
    repository.ensure_seed()
    repository.recover_interrupted()
    generation_id = repository.begin_inventory(kind=kind)
    try:
        _report(progress, stage="discover")
        inventory = discover_sources(source)
        repository.save_inventory(generation_id, inventory, finalize=False)
        work = _work_items(inventory)
        _report(
            progress,
            stage="inventory",
            supported=inventory.supported_file_count,
            unsupported=inventory.unsupported_file_count,
            skipped=sum(record.status == DiscoveryStatus.SKIPPED for record in inventory.records),
            failed=sum(record.status == DiscoveryStatus.FAILED for record in inventory.records),
            unique=len(work),
            formats=tuple(record.format.value for record, _identity, _copies in work),
        )
        configuration = reader_fingerprint()
        for index, (record, identity, copies) in enumerate(work, start=1):
            started = time.monotonic()
            key = cache_key(*identity, configuration)
            result = verified_cache(data, repository.cached_reader(key), *identity, configuration)
            cache_hit = result is not None
            if result is None:
                _report(
                    progress,
                    stage="document",
                    index=index,
                    count=len(work),
                    source=record.relative_path,
                    copies=copies,
                    format=record.format.value,
                )
                try:
                    result = read_document(source, record, data)
                except ReaderError as error:
                    evidence = DocumentEvidence(ProcessingStatus.FAILED, (str(error),), None)
                else:
                    evidence = result.evidence
                    if cache_eligible(result):
                        repository.cache_reader(
                            key,
                            *identity,
                            configuration,
                            result.reference,
                            artifact_checksums(data, result.reference, result),
                        )
            else:
                evidence = result.evidence
                _report(
                    progress,
                    stage="document",
                    index=index,
                    count=len(work),
                    source=record.relative_path,
                    copies=copies,
                    format=record.format.value,
                    cached=True,
                    pages=evidence.page_count,
                )
            if kind == "scan" and result is not None:
                status, file_warnings, units = process_content(
                    repository, data, result.reference, *identity, evidence, ocr, vision, progress
                )
                warnings = sorted(set(file_warnings))
                shown = tuple(
                    sorted(set(file_warnings) | {item for unit in units for item in unit.warnings})
                )
                unit_count = len(units)
            else:
                status, units, unit_count = evidence.status.value, (), len(evidence.units)
                warnings = sorted(
                    {
                        *evidence.warnings,
                        *(warning for unit in evidence.units for warning in unit.warnings),
                    }
                )
                shown = tuple(warnings)
            repository.record_read(
                generation_id,
                *identity,
                result.reference if result else None,
                status,
                warnings,
                evidence.page_count,
                unit_count,
                cache_hit,
                units,
            )
            _report(
                progress,
                stage="recorded",
                status=status,
                warnings=shown,
                elapsed_s=int(time.monotonic() - started),
                format=record.format.value,
                pages=evidence.page_count,
                cached=cache_hit,
            )
        if kind == "scan":
            _report(progress, stage="mappings")
            repository.propose_mappings(generation_id)
        _report(progress, stage="recheck")
        repository.finish_scan(generation_id, discover_sources(source))
        return generation_id
    except BaseException:
        repository.interrupt(generation_id)
        raise


@dataclass(frozen=True)
class ScanOutcome:
    generation_id: UUID
    activation: ActivationResult | None
    blocked: str | None = None


def run_full_scan(
    repository: Repository,
    source: Path,
    data: Path,
    ocr: OcrEngine | None,
    vision: VisionClient | None,
    progress: ProgressFn | None = None,
) -> ScanOutcome:
    """Full scan followed by automatic activation under the guard (D10, D35, D42)."""
    generation_id = scan_sources(repository, source, data, "scan", ocr, vision, progress)
    generation = next(item for item in repository.generations() if item.id == generation_id)
    if generation.state != "staged":
        return ScanOutcome(generation_id, None, generation.reason)
    try:
        _report(progress, stage="activate")
        return ScanOutcome(
            generation_id, repository.activate(generation_id, discover_sources(source))
        )
    except DatabaseError as error:
        return ScanOutcome(generation_id, None, str(error))
