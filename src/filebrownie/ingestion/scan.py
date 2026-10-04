"""Serialized reader-only generations. No OCR, medical interpretation, or activation."""

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
from filebrownie.evidence.network import ensure_isolated_network
from filebrownie.evidence.readers import ReaderError, read_document
from filebrownie.ingestion.discovery import DiscoveryStatus, discover_sources
from filebrownie.storage.database import Repository


def scan_sources(repository: Repository, source: Path, data: Path) -> UUID:
    """The caller must hold the shared application lock throughout this operation."""
    ensure_isolated_network()
    repository.require_schema()
    repository.recover_interrupted()
    generation_id = repository.begin_inventory(kind="reader")
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
                evidence.status.value,
                warnings,
                evidence.page_count,
                len(evidence.units),
                cache_hit,
            )
        repository.finish_reader_scan(generation_id, discover_sources(source))
        return generation_id
    except BaseException:
        repository.interrupt(generation_id)
        raise
