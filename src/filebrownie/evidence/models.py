"""Domain-independent located evidence and reader completion, never medical facts."""

from dataclasses import asdict, dataclass
from enum import StrEnum


class ProcessingStatus(StrEnum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class TextSpan:
    text: str
    bbox: tuple[float, float, float, float] | None
    reader: str = "pdf-text"


@dataclass(frozen=True)
class UnitEvidence:
    number: int
    status: ProcessingStatus
    warnings: tuple[str, ...]
    spans: tuple[TextSpan, ...] = ()
    raster: str | None = None
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True)
class DocumentEvidence:
    status: ProcessingStatus
    warnings: tuple[str, ...]
    page_count: int | None
    units: tuple[UnitEvidence, ...] = ()
    reader_version: str = "1"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "DocumentEvidence":
        return cls(
            ProcessingStatus(data["status"]),
            tuple(data["warnings"]),
            data["page_count"],
            tuple(
                UnitEvidence(
                    unit["number"],
                    ProcessingStatus(unit["status"]),
                    tuple(unit["warnings"]),
                    tuple(
                        TextSpan(
                            span["text"],
                            tuple(span["bbox"]) if span["bbox"] else None,
                            span["reader"],
                        )
                        for span in unit["spans"]
                    ),
                    unit["raster"],
                    unit["width"],
                    unit["height"],
                )
                for unit in data["units"]
            ),
            data["reader_version"],
        )
