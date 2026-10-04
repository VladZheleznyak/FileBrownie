"""Deterministic date-range filtering that preserves precision and ambiguity (D21, D34)."""

import calendar
import re
from dataclasses import dataclass
from datetime import date

from filebrownie.interpretation.dates import DateValue

UNDATED = "undated"
IN_RANGE = "in range"
MAY_FALL = "may fall within range"
UNCERTAIN = "date-uncertain"
OUT = "out of range"


class RangeError(ValueError):
    """Fixed code for an unreadable --from/--to value."""


@dataclass(frozen=True)
class DateRange:
    start: date | None = None
    end: date | None = None

    @property
    def active(self) -> bool:
        return self.start is not None or self.end is not None


def parse_bound(text: str, upper: bool) -> date:
    """YYYY-MM-DD, YYYY-MM, or YYYY. A lower bound starts the period; an upper bound ends it."""
    match = re.fullmatch(r"(\d{4})(?:-(\d{1,2})(?:-(\d{1,2}))?)?", text.strip())
    if not match:
        raise RangeError("INVALID_DATE_RANGE")
    year = int(match[1])
    month = int(match[2]) if match[2] else (12 if upper else 1)
    try:
        if match[3]:
            return date(year, month, int(match[3]))
        last = calendar.monthrange(year, month)[1]
        return date(year, month, last if upper else 1)
    except ValueError:
        raise RangeError("INVALID_DATE_RANGE") from None


def parse_range(start: str | None, end: str | None) -> DateRange:
    result = DateRange(
        parse_bound(start, False) if start else None, parse_bound(end, True) if end else None
    )
    if result.start and result.end and result.start > result.end:
        raise RangeError("INVALID_DATE_RANGE")
    return result


def _overlaps(item: DateValue, window: DateRange) -> bool:
    return (window.start is None or item.end >= window.start) and (
        window.end is None or item.start <= window.end
    )


def _contained(item: DateValue, window: DateRange) -> bool:
    return (window.start is None or item.start >= window.start) and (
        window.end is None or item.end <= window.end
    )


def classify(alternatives: tuple[DateValue, ...], window: DateRange) -> str:
    """Place a row on the timeline relative to the filter.

    More than one alternative is always date-uncertain when any overlaps (or when no filter
    applies). A single partial-precision reading that only overlaps the range may fall within it.
    """
    if not alternatives:
        return UNDATED
    if len(alternatives) > 1:
        if not window.active or any(_overlaps(item, window) for item in alternatives):
            return UNCERTAIN
        return OUT
    (item,) = alternatives
    if not _overlaps(item, window):
        return OUT
    return IN_RANGE if _contained(item, window) else MAY_FALL


def format_date(item: DateValue) -> str:
    if item.precision == "year":
        return f"{item.start.year}"
    if item.precision == "month":
        return f"{item.start.year}-{item.start.month:02d}"
    return item.start.isoformat()


def format_alternatives(alternatives: tuple[DateValue, ...]) -> str:
    return " or ".join(format_date(item) for item in alternatives)
