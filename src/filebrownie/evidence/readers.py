"""Bounded readers run in an isolated process on a fingerprint-checked local copy."""

import hashlib
import json
import os
import stat
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import UUID, uuid4

from filebrownie.evidence.models import DocumentEvidence, ProcessingStatus, UnitEvidence
from filebrownie.evidence.network import ensure_isolated_network
from filebrownie.ingestion.discovery import CHUNK_SIZE, SourceFormat, SourceRecord

MAX_INPUT_BYTES = 64 * 1024 * 1024
WORKER_TIMEOUT_SECONDS = 90
MAX_MANIFEST_BYTES = 16 * 1024 * 1024


class ReaderError(Exception):
    """Fixed safe codes only; raw parser errors never escape."""


@dataclass(frozen=True)
class ReadResult:
    reference: str
    source: str
    content_hash: str
    evidence: DocumentEvidence


def _copy_source(root: Path, record: SourceRecord, target: Path) -> None:
    parts = Path(record.relative_path).parts
    if not parts or Path(record.relative_path).is_absolute() or ".." in parts:
        raise ReaderError("INVALID_SOURCE_REFERENCE")
    directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child_fd = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd
            )
            os.close(directory_fd)
            directory_fd = child_fd
        descriptor = os.open(
            parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd
        )
        with os.fdopen(descriptor, "rb") as source, target.open("xb") as destination:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ReaderError("SOURCE_NOT_REGULAR")
            digest = hashlib.sha256()
            size = 0
            while chunk := source.read(CHUNK_SIZE):
                size += len(chunk)
                if size > MAX_INPUT_BYTES:
                    raise ReaderError("INPUT_SIZE_LIMIT")
                digest.update(chunk)
                destination.write(chunk)
            if digest.hexdigest() != record.content_hash:
                raise ReaderError("SOURCE_CHANGED")
    finally:
        os.close(directory_fd)


def _load_result(directory: Path) -> DocumentEvidence:
    try:
        if directory.joinpath("manifest.json").stat().st_size > MAX_MANIFEST_BYTES:
            raise ValueError
        return DocumentEvidence.from_dict(
            json.loads(directory.joinpath("manifest.json").read_text())
        )
    except (OSError, ValueError, KeyError, TypeError):
        return DocumentEvidence(ProcessingStatus.FAILED, ("READER_RESULT_UNAVAILABLE",), None)


def _interrupted(result: DocumentEvidence, warning: str) -> DocumentEvidence:
    units = result.units
    if result.page_count is not None and len(units) < result.page_count:
        units += (UnitEvidence(len(units) + 1, ProcessingStatus.FAILED, (warning,)),)
    retained = any(unit.raster is not None or unit.spans for unit in units)
    return replace(
        result,
        status=ProcessingStatus.PARTIAL if retained else ProcessingStatus.FAILED,
        warnings=tuple(dict.fromkeys((*result.warnings, warning))),
        units=units,
    )


def read_document(root: Path, record: SourceRecord, data: Path) -> ReadResult:
    if record.format not in (SourceFormat.PDF, SourceFormat.JPEG) or record.content_hash is None:
        raise ReaderError("SOURCE_NOT_READABLE_OR_UNSUPPORTED")
    ensure_isolated_network()
    evidence_root = data / "evidence"
    evidence_root.mkdir(mode=0o700, exist_ok=True)
    reference = str(uuid4())
    directory = evidence_root / reference
    directory.mkdir(mode=0o700)
    source_copy = directory / "input"
    try:
        _copy_source(root, record, source_copy)
        try:
            process = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "filebrownie.evidence.worker",
                    str(directory),
                    record.format,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=WORKER_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            result = _interrupted(_load_result(directory), "READER_TIMEOUT")
        else:
            result = _load_result(directory)
            if process.returncode != 0:
                result = _interrupted(result, "READER_PROCESS_FAILED")
        output = {
            "source": record.relative_path,
            "content_hash": record.content_hash,
            "evidence": result.to_dict(),
        }
        directory.joinpath("result.json").write_text(json.dumps(output, ensure_ascii=True))
        return ReadResult(reference, record.relative_path, record.content_hash, result)
    except OSError:
        raise ReaderError("READER_STORAGE_OR_SOURCE_UNAVAILABLE") from None
    finally:
        source_copy.unlink(missing_ok=True)


def inspect_evidence(data: Path, reference: str) -> ReadResult:
    try:
        if str(UUID(reference)) != reference:
            raise ValueError
        path = data / "evidence" / reference / "result.json"
        if path.stat().st_size > MAX_MANIFEST_BYTES + 4096:
            raise ValueError
        result = json.loads(path.read_text())
        return ReadResult(
            reference,
            result["source"],
            result["content_hash"],
            DocumentEvidence.from_dict(result["evidence"]),
        )
    except (OSError, ValueError, KeyError, TypeError):
        raise ReaderError("EVIDENCE_UNAVAILABLE") from None
