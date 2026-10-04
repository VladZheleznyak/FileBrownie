"""Serialized scans: `reader` stops after readers; `scan` runs the full pipeline."""

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
from filebrownie.interpretation.pipeline import process_content
from filebrownie.interpretation.vision import VisionClient
from filebrownie.storage.database import ActivationResult, DatabaseError, Repository


def scan_sources(
    repository: Repository,
    source: Path,
    data: Path,
    kind: str = "reader",
    ocr: OcrEngine | None = None,
    vision: VisionClient | None = None,
) -> UUID:
    """The caller must hold the shared application lock throughout this operation."""
    ensure_isolated_network()
    if kind == "scan":
        ensure_no_outbound()  # medical processing refuses to run with internet access (D13)
    repository.require_schema()
    repository.ensure_seed()
    repository.recover_interrupted()
    generation_id = repository.begin_inventory(kind=kind)
    try:
        inventory = discover_sources(source)
        repository.save_inventory(generation_id, inventory, finalize=False)
        configuration = reader_fingerprint()
        processed = set()
        for record in inventory.records:
            if record.status != DiscoveryStatus.READY or record.content_hash is None:
                continue
            identity = (record.content_hash, record.format.value)
            if identity in processed:
                continue
            processed.add(identity)
            key = cache_key(*identity, configuration)
            result = verified_cache(data, repository.cached_reader(key), *identity, configuration)
            cache_hit = result is not None
            if result is None:
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
            if kind == "scan" and result is not None:
                status, file_warnings, units = process_content(
                    repository, data, result.reference, *identity, evidence, ocr, vision
                )
                warnings = sorted(set(file_warnings))
                unit_count = len(units)
            else:
                status, units, unit_count = evidence.status.value, (), len(evidence.units)
                warnings = sorted(
                    {
                        *evidence.warnings,
                        *(warning for unit in evidence.units for warning in unit.warnings),
                    }
                )
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
        if kind == "scan":
            repository.propose_mappings(generation_id)
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
) -> ScanOutcome:
    """Full scan followed by automatic activation under the guard (D10, D35, D42)."""
    generation_id = scan_sources(repository, source, data, "scan", ocr, vision)
    generation = next(item for item in repository.generations() if item.id == generation_id)
    if generation.state != "staged":
        return ScanOutcome(generation_id, None, generation.reason)
    try:
        return ScanOutcome(
            generation_id, repository.activate(generation_id, discover_sources(source))
        )
    except DatabaseError as error:
        return ScanOutcome(generation_id, None, str(error))
