"""Versioned medical facts linked to located evidence. Raw labels stay as written (D16)."""

from dataclasses import dataclass, field

from filebrownie.interpretation.dates import DateValue

EXTRACTOR_VERSION = "interpretation-4"

VERIFIED = "verified"
UNVERIFIED = "unverified reading"
CONFLICTING = "conflicting"


@dataclass(frozen=True)
class ReportedDate:
    """One date as written, with role, supported readings, and where it was found."""

    raw: str
    role: str
    alternatives: tuple[DateValue, ...]
    evidence: tuple[int, ...]
    planned: bool = False
    role_supported: bool = True

    @property
    def grounded(self) -> bool:
        return bool(self.evidence)

    def as_json(self) -> dict:
        return {
            "raw": self.raw,
            "role": self.role,
            "planned": self.planned,
            "role_supported": self.role_supported,
            "alternatives": [item.as_json() for item in self.alternatives],
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True)
class Timeline:
    """The date used for ordering/filtering. Several alternatives mean date-uncertain."""

    role: str | None
    alternatives: tuple[DateValue, ...]

    @property
    def envelope(self) -> tuple[DateValue, DateValue] | None:
        if not self.alternatives:
            return None
        return (
            min(self.alternatives, key=lambda item: item.start),
            max(self.alternatives, key=lambda item: item.end),
        )


@dataclass(frozen=True)
class LabFact:
    raw_label: str
    raw_value: str
    comparator: str | None
    value_number: str | None
    qualitative: bool
    unit: str | None
    reference_interval: str | None
    flag: str | None
    specimen: str | None
    dates: tuple[ReportedDate, ...]
    timeline: Timeline
    verification: str
    evidence: tuple[int, ...]
    alternative_label: str | None = None
    alternative_evidence: tuple[int, ...] = ()
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class EventFact:
    raw_specialty: str
    event_type: str
    recommendation: bool
    wording: str
    planned: bool
    strength: str  # direct | indirect | weak
    dates: tuple[ReportedDate, ...]
    timeline: Timeline
    verification: str
    evidence: tuple[int, ...]
    alternative_event_type: str | None = None
    alternative_evidence: tuple[int, ...] = ()
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class InterpretationWarning:
    code: str
    evidence: tuple[int, ...] = ()


@dataclass(frozen=True)
class UnitInterpretation:
    document_class: str
    lab_facts: tuple[LabFact, ...] = ()
    events: tuple[EventFact, ...] = ()
    warnings: tuple[InterpretationWarning, ...] = field(default_factory=tuple)
