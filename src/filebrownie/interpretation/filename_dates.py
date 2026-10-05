"""Conservative dates inferred from source paths when the document body has no timeline."""

import re
from collections.abc import Sequence
from pathlib import PurePosixPath

from filebrownie.interpretation.dates import DateValue, parse_date

_ISO_FRAGMENT = re.compile(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)")
_DOT_DATE = re.compile(r"(?<!\d)(\d{4}\.\d{2}\.\d{2})(?!\d)")
_COMPACT_DATE = re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})(?=[^\d]|$)")


def _single_reading(parsed: tuple[DateValue, ...]) -> tuple[DateValue, ...]:
    return parsed if len(parsed) == 1 else ()


def _dates_from_segment(segment: str) -> tuple[DateValue, ...]:
    for match in _DOT_DATE.finditer(segment):
        year, month, day = (int(part) for part in match.group(1).split("."))
        found = _single_reading(parse_date(f"{year:04d}-{month:02d}-{day:02d}"))
        if found:
            return found
    for match in _ISO_FRAGMENT.finditer(segment):
        found = _single_reading(parse_date(match.group(1)))
        if found:
            return found
    for match in _COMPACT_DATE.finditer(segment):
        found = _single_reading(parse_date(f"{match[1]}-{match[2]}-{match[3]}"))
        if found:
            return found
    return _single_reading(parse_date(segment))


def dates_from_path(relative_path: str) -> tuple[DateValue, ...]:
    """Return one unambiguous date read from path segments, basename first."""
    for part in reversed(PurePosixPath(relative_path).parts):
        found = _dates_from_segment(part)
        if found:
            return found
    return ()


def consensus_filename_dates(source_paths: Sequence[str]) -> tuple[DateValue, ...]:
    """One inferred period when every path agrees; empty when paths conflict or lack a date."""
    if not source_paths:
        return ()
    readings = [dates_from_path(path) for path in sorted(source_paths)]
    readings = [item for item in readings if item]
    if not readings:
        return ()
    first = readings[0]
    if all(item == first for item in readings):
        return first
    return ()
