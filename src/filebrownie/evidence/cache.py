"""Exact reader cache identity and local artifact integrity checks."""

import hashlib
import json
import platform
from importlib.metadata import version
from pathlib import Path

from filebrownie.evidence import readers, worker
from filebrownie.evidence.readers import ReaderError, ReadResult, inspect_evidence
from filebrownie.ingestion.discovery import CHUNK_SIZE


def reader_fingerprint() -> str:
    package = Path(__file__).parent
    configuration = {
        "code": {
            name: hashlib.sha256(package.joinpath(name).read_bytes()).hexdigest()
            for name in ("models.py", "readers.py", "worker.py")
        },
        "packages": {name: version(name) for name in ("PyMuPDF", "Pillow")},
        "python": platform.python_version(),
        "machine": platform.machine(),
        "limits": {
            module.__name__: {
                name: value
                for name, value in vars(module).items()
                if name.isupper() and isinstance(value, (str, int, float))
            }
            for module in (readers, worker)
        },
    }
    return hashlib.sha256(json.dumps(configuration, sort_keys=True).encode()).hexdigest()


def cache_key(content_hash: str, format: str, configuration_hash: str) -> str:
    return hashlib.sha256(
        json.dumps([content_hash, format, configuration_hash], separators=(",", ":")).encode()
    ).hexdigest()


def cache_eligible(result: ReadResult) -> bool:
    evidence = result.evidence
    allowed_warnings = {
        "TEXT_LAYER_COVERAGE_UNVERIFIED",
        "TEXT_LOCATION_UNAVAILABLE",
        "TEXT_LAYER_LOW_QUALITY",
        "OCR_REQUIRED",
        "IMAGE_ORIENTATION_APPLIED",
        "PDF_REPAIRED",
    }
    return (
        evidence.status in ("completed", "partial")
        and evidence.page_count is not None
        and len(evidence.units) == evidence.page_count
        and all(
            unit.status in ("completed", "partial")
            and unit.raster is not None
            and set(unit.warnings) <= allowed_warnings
            for unit in evidence.units
        )
        and set(evidence.warnings) <= allowed_warnings
    )


def artifact_checksums(data: Path, reference: str, result: ReadResult) -> dict[str, str]:
    base = data / "evidence" / reference
    if base.is_symlink() or base.parent.is_symlink():
        raise ReaderError("CACHE_ARTIFACT_UNAVAILABLE")
    paths = {"result.json", *(unit.raster for unit in result.evidence.units if unit.raster)}
    checksums = {}
    for name in sorted(paths):
        path = Path(name)
        if path.is_absolute() or ".." in path.parts:
            raise ReaderError("CACHE_ARTIFACT_UNAVAILABLE")
        artifact = base / path
        if any(
            (base.joinpath(*path.parts[:index])).is_symlink()
            for index in range(1, len(path.parts) + 1)
        ):
            raise ReaderError("CACHE_ARTIFACT_UNAVAILABLE")
        if artifact.is_symlink() or not artifact.resolve().is_relative_to(base.resolve()):
            raise ReaderError("CACHE_ARTIFACT_UNAVAILABLE")
        if not artifact.is_file() or artifact.stat().st_size > worker.MAX_ARTIFACT_BYTES:
            raise ReaderError("CACHE_ARTIFACT_UNAVAILABLE")
        digest = hashlib.sha256()
        with artifact.open("rb") as stream:
            while chunk := stream.read(CHUNK_SIZE):
                digest.update(chunk)
        checksums[name] = digest.hexdigest()
    return checksums


def verified_cache(
    data: Path, entry: dict | None, content_hash: str, format: str, configuration_hash: str
) -> ReadResult | None:
    if entry is None or (
        entry["content_hash"] != content_hash
        or entry["format"] != format
        or entry["configuration_hash"] != configuration_hash
    ):
        return None
    try:
        reference = str(entry["reference"])
        # Check the result JSON before interpreting any of its declared artifact paths.
        result_path = data / "evidence" / reference / "result.json"
        if any(
            path.is_symlink()
            for path in (result_path, result_path.parent, result_path.parent.parent)
        ):
            return None
        if result_path.stat().st_size > readers.MAX_MANIFEST_BYTES + 4096:
            return None
        if (
            hashlib.sha256(result_path.read_bytes()).hexdigest()
            != entry["artifacts"]["result.json"]
        ):
            return None
        result = inspect_evidence(data, reference)
        if result.content_hash != content_hash or not cache_eligible(result):
            return None
        return result if artifact_checksums(data, reference, result) == entry["artifacts"] else None
    except (OSError, ValueError, KeyError, TypeError, ReaderError):
        return None
