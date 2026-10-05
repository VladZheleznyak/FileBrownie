"""Vision-extraction contract: untrusted structured output for one page image.

The model output is a claim to be grounded in located text, never evidence by itself.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

MAX_ROWS = 400
MAX_EVENTS = 50
MAX_DATES = 20
MAX_TEXT = 300

DOCUMENT_CLASSES = (
    "lab_report",
    "visit_note",
    "referral",
    "appointment",
    "imaging_report",
    "discharge",
    "invoice",
    "other",
)
DATE_ROLES = ("specimen", "report", "event", "unspecified")
EVENT_TYPES = (
    "referral",
    "appointment_scheduled",
    "appointment_confirmed",
    "encounter",
    "procedure",
    "result",
    "discharge",
    "invoice",
    "other",
)


class VisionError(Exception):
    """Fixed safe codes only; model/runtime diagnostics never escape."""


class VisionClient(Protocol):
    """A local model client. `version` identifies model, prompt, schema, and configuration."""

    version: str

    def extract(self, image_png: bytes, *, lab_row_hints: Sequence[str] | None = None) -> dict: ...


@dataclass(frozen=True)
class VisionDate:
    raw: str
    role: str


@dataclass(frozen=True)
class VisionLabRow:
    label: str
    value: str
    unit: str | None
    reference_interval: str | None
    flag: str | None
    specimen: str | None
    dates: tuple[VisionDate, ...]


@dataclass(frozen=True)
class VisionEvent:
    specialty: str
    event_type: str
    wording: str
    planned: bool
    recommendation: bool
    dates: tuple[VisionDate, ...]


@dataclass(frozen=True)
class VisionPage:
    document_class: str
    handwriting: bool
    context_missing: bool
    dates: tuple[VisionDate, ...]
    lab_rows: tuple[VisionLabRow, ...]
    events: tuple[VisionEvent, ...]
    dropped: int = 0  # items without their required text; never treated as claims


def _text(value, required: bool = False) -> str | None:
    if value is None or value == "":
        if required:
            raise VisionError("VISION_OUTPUT_INVALID")
        return None
    if not isinstance(value, str):
        raise VisionError("VISION_OUTPUT_INVALID")
    value = value.strip()[:MAX_TEXT]
    if required and not value:
        raise VisionError("VISION_OUTPUT_INVALID")
    return value or None


def _bool(value) -> bool:
    if value is None:
        return False
    if not isinstance(value, bool):
        raise VisionError("VISION_OUTPUT_INVALID")
    return value


def _list(value, limit: int) -> list:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > limit:
        raise VisionError("VISION_OUTPUT_INVALID")
    return value


_PLACEHOLDER_VALUES = frozenset(
    {
        "-",
        "—",
        "–",
        "na",
        "n/a",
        "pending",
        "cancelled",
        "canceled",
        "x",
        "xx",
        "xxx",
        "tbd",
        "none",
    }
)


def _is_placeholder_lab_value(value: str) -> bool:
    token = value.casefold().strip()
    if not token:
        return True
    return token in _PLACEHOLDER_VALUES


_INTEGER_TRAILING_PERIOD = re.compile(r"^\d+\.$")


def _normalize_lab_value(value: str) -> str:
    stripped = value.strip()
    if _INTEGER_TRAILING_PERIOD.match(stripped):
        return stripped[:-1]
    return value


def _dates(value) -> tuple[VisionDate, ...]:
    result = []
    for item in _list(value, MAX_DATES):
        if not isinstance(item, dict):
            raise VisionError("VISION_OUTPUT_INVALID")
        role = _text(item.get("role")) or "unspecified"
        result.append(
            VisionDate(
                _text(item.get("raw"), required=True),
                role if role in DATE_ROLES else "unspecified",
            )
        )
    return tuple(result)


def parse_page(data: dict) -> VisionPage:
    """Validate and bound model output; unknown values degrade to the conservative option."""
    if not isinstance(data, dict):
        raise VisionError("VISION_OUTPUT_INVALID")
    document_class = _text(data.get("document_class")) or "other"
    rows, dropped = [], 0
    for item in _list(data.get("lab_rows"), MAX_ROWS):
        if not isinstance(item, dict):
            raise VisionError("VISION_OUTPUT_INVALID")
        label, value = _text(item.get("label")), _text(item.get("value"))
        if not label or not value:
            dropped += bool(label or value)  # an all-empty item is a placeholder, not a loss
            continue
        if _is_placeholder_lab_value(value):
            dropped += 1
            continue
        value = _normalize_lab_value(value)
        rows.append(
            VisionLabRow(
                label,
                value,
                _text(item.get("unit")),
                _text(item.get("reference_interval")),
                _text(item.get("flag")),
                _text(item.get("specimen")),
                _dates(item.get("dates")),
            )
        )
    events = []
    for item in _list(data.get("events"), MAX_EVENTS):
        if not isinstance(item, dict):
            raise VisionError("VISION_OUTPUT_INVALID")
        specialty = _text(item.get("specialty"))
        if not specialty:
            dropped += bool(_text(item.get("wording")) or item.get("dates"))
            continue
        event_type = _text(item.get("event_type")) or "other"
        events.append(
            VisionEvent(
                specialty,
                event_type if event_type in EVENT_TYPES else "other",
                _text(item.get("wording")) or "",
                _bool(item.get("planned")),
                _bool(item.get("recommendation")),
                _dates(item.get("dates")),
            )
        )
    return VisionPage(
        document_class if document_class in DOCUMENT_CLASSES else "other",
        _bool(data.get("handwriting")),
        _bool(data.get("context_missing")),
        _dates(data.get("dates")),
        tuple(rows),
        tuple(events),
        dropped,
    )


def merge_vision_lab_rows(base: VisionPage, extra: VisionPage) -> VisionPage:
    """Append supplemental lab rows while keeping the first page's other fields."""
    return VisionPage(
        base.document_class,
        base.handwriting or extra.handwriting,
        base.context_missing or extra.context_missing,
        base.dates,
        (*base.lab_rows, *extra.lab_rows),
        base.events,
        base.dropped + extra.dropped,
    )
