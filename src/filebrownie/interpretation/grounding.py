"""Locate claims in text spans. A matching number elsewhere on the page proves nothing (D4, D28).

Spans carry indices into the unit's span list; rows are derived from bounding boxes.
Spans without a usable box are never merged into rows, so they can only ground claims
that sit entirely inside a single span.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from filebrownie.evidence.models import TextSpan
from filebrownie.evidence.normalize import contains_token, fold_signs, normalize

_COMPARATOR_GAP = re.compile(r"([<>≤≥]=?)\s+(?=[+\-]?\d)")
_EDGE = " :;,.|"


def clean(text: str | None) -> str:
    """Normalize a claimed string for matching; empty when nothing matchable remains."""
    if not text:
        return ""
    folded = fold_signs(normalize(text))
    return _COMPARATOR_GAP.sub(r"\1", folded).strip(_EDGE)


@dataclass(frozen=True)
class Row:
    indices: tuple[int, ...]  # span indices ordered left to right
    text: str  # normalized, joined
    y: float | None  # vertical centre; None for spans without a location
    height: float = 0.0


@dataclass(frozen=True)
class PageText:
    spans: tuple[TextSpan, ...]
    normalized: tuple[str, ...]
    rows: tuple[Row, ...]

    @classmethod
    def build(cls, spans: Sequence[TextSpan]) -> "PageText":
        spans = tuple(spans)
        normalized = tuple(clean(span.text) for span in spans)
        located = [
            (index, (span.bbox[1] + span.bbox[3]) / 2, span.bbox[3] - span.bbox[1], span.bbox[0])
            for index, span in enumerate(spans)
            if span.bbox is not None
        ]
        located.sort(key=lambda item: (item[1], item[3]))
        groups: list[list[tuple[int, float, float, float]]] = []
        for item in located:
            if groups:
                centre = sum(member[1] for member in groups[-1]) / len(groups[-1])
                height = max(member[2] for member in groups[-1])
                if abs(item[1] - centre) <= 0.5 * max(height, item[2]):
                    groups[-1].append(item)
                    continue
            groups.append([item])
        rows = []
        for group in groups:
            ordered = sorted(group, key=lambda member: member[3])
            indices = tuple(member[0] for member in ordered)
            joined = clean(" ".join(spans[i].text for i in indices))
            rows.append(
                Row(
                    indices, joined, sum(m[1] for m in group) / len(group), max(m[2] for m in group)
                )
            )
        for index, span in enumerate(spans):
            if span.bbox is None and normalized[index]:
                rows.append(Row((index,), normalized[index], None))
        return cls(spans, normalized, tuple(rows))

    def rows_with(self, needle: str) -> list[Row]:
        return [row for row in self.rows if contains_token(row.text, needle)]

    def refs(self, row: Row, needle: str) -> tuple[int, ...]:
        """Spans of `row` that carry `needle`; the whole row when it spans several spans."""
        exact = tuple(
            i
            for i in row.indices
            if self.normalized[i] and contains_token(self.normalized[i], needle)
        )
        if exact:
            return exact
        partial = tuple(
            i for i in row.indices if self.normalized[i] and self.normalized[i] in needle
        )
        return partial or row.indices

    def above(self, row: Row) -> list[Row]:
        if row.y is None:
            return []
        nearer_first = sorted(
            (other for other in self.rows if other.y is not None and other.y < row.y),
            key=lambda other: row.y - other.y,
        )
        return nearer_first

    def locate(self, needle: str) -> tuple[int, ...]:
        """Evidence for a claimed string anywhere on the page, empty when not located."""
        needle = clean(needle)
        for row in self.rows_with(needle):
            return self.refs(row, needle)
        return ()


@dataclass(frozen=True)
class LabGrounding:
    verification: str
    evidence: tuple[int, ...]
    alternative_label: str | None = None
    alternative_evidence: tuple[int, ...] = ()
    notes: tuple[str, ...] = ()


_UNIT_LIKE = re.compile(r"(?:[a-zа-яіїєґµμ%]+[a-zа-яіїєґ0-9^*]*/[a-zа-яіїєґ0-9^*.]+)|%")
_INTERVAL = re.compile(r"\d+(?:\.\d+)?\s*[-–—]\s*\d+(?:\.\d+)?")
# Two-character comparators and an explicit sign are part of the result token.
# A following digit must not match on its own inside <=5, +2, or −5.
_STANDALONE_NUMBER = re.compile(r"(?<![\w.])(?:<=|>=|<|>|≤|≥)?[+\-]?\d+(?:\.\d+)?(?![\w.])")


def looks_like_lab_row(row: Row) -> bool:
    """Heuristic for a laboratory table row: label, a standalone value, and a unit or interval."""
    text = row.text
    has_interval = _INTERVAL.search(text) is not None
    stripped = _INTERVAL.sub(" ", text)
    if not re.match(r"^\W*[^\W\d_]{3,}", stripped):
        return False
    if _STANDALONE_NUMBER.search(stripped) is None:
        return False
    return has_interval or _UNIT_LIKE.search(stripped) is not None


def _text_after_label(row_text: str, label_n: str) -> str | None:
    if not label_n or not contains_token(row_text, label_n):
        return None
    start = row_text.find(label_n)
    if start < 0:
        return None
    return row_text[start + len(label_n) :]


def _first_result_token(text: str) -> str | None:
    """First standalone value token after the label, ignoring reference intervals."""
    stripped = _INTERVAL.sub(" ", text)
    match = _STANDALONE_NUMBER.search(stripped)
    if not match:
        return None
    return clean(match.group(0))


_REFERENCE_FIELD = re.compile(
    r"\b(?:reference|ref(?:erence)?|norm(?:al)?|referencia|референс|норма)\b",
    re.IGNORECASE,
)


def _result_field_text(after: str) -> str:
    """Portion of a row after the label, stopping before reference annotations."""
    match = _REFERENCE_FIELD.search(after)
    if match:
        after = after[: match.start()]
    return after.strip(_EDGE)


def _first_qualitative_token(after: str) -> str | None:
    field = _result_field_text(after)
    if not field:
        return None
    segment = field.split("|", 1)[0].strip(_EDGE)
    token = clean(segment)
    return token or None


def value_at_result_position(row: Row, label_n: str, value_n: str) -> bool:
    """True when `value_n` matches the first result-field token to the right of the label."""
    if not value_n or not label_n:
        return False
    after = _text_after_label(row.text, label_n)
    if after is None:
        return False
    located = _first_result_token(after)
    if located is None:
        if any(character.isdigit() for character in value_n):
            return False
        located = _first_qualitative_token(after)
        if located is None:
            return False
        return located == value_n
    return located == value_n


def _header_rows_above(page: PageText, row: Row) -> list[Row]:
    """Non-result rows above `row`, nearest first; only these may supply inherited unit/specimen."""
    return [other for other in page.above(row) if not looks_like_lab_row(other)]


def _inherit_field(
    page: PageText, row: Row, needle: str, *, from_header: bool
) -> tuple[Row, str] | None:
    if contains_token(row.text, needle):
        return (row, needle)
    # A unit already written on the result row is the result unit. A different unit in a
    # header must not replace it. Specimen text is still taken from a header when the row
    # does not state one.
    if not from_header:
        return None
    headers = _header_rows_above(page, row)
    return next(
        ((header, needle) for header in headers if contains_token(header.text, needle)),
        None,
    )


def _geometric_label(page: PageText, row: Row, value: str) -> tuple[str, tuple[int, ...]]:
    """Text to the left of the value span in its row, as written."""
    parts, refs = [], []
    for index in row.indices:
        if contains_token(page.normalized[index], value):
            break
        parts.append(page.spans[index].text.strip())
        refs.append(index)
    return " ".join(part for part in parts if part).strip(_EDGE), tuple(refs)


def ground_lab_row(
    page: PageText,
    label: str,
    value: str,
    unit: str | None,
    specimen: str | None,
) -> LabGrounding:
    """Verify the full association: label, value and any reported unit/specimen in one row.

    Unit and specimen may be grounded in a header row above the data row. Dates are checked
    separately by the caller. Readers that place the value under a different label produce
    a conflicting candidate holding both interpretations.
    """
    label_n, value_n = clean(label), clean(value)
    if not page.spans or not label_n or not value_n:
        return LabGrounding("unverified reading", (), notes=("NOT_LOCATED",))
    label_rows, value_rows = page.rows_with(label_n), page.rows_with(value_n)
    shared = [row for row in label_rows if row in value_rows]
    if shared:
        row = shared[0]
        if not value_at_result_position(row, label_n, value_n):
            return LabGrounding(
                "unverified reading",
                tuple(sorted(page.refs(row, label_n))),
                notes=("VALUE_NOT_AT_RESULT",),
            )
        evidence = set(page.refs(row, label_n)) | set(page.refs(row, value_n))
        notes = []
        for claimed, name in ((unit, "UNIT"), (specimen, "SPECIMEN")):
            needle = clean(claimed)
            if not needle:
                continue
            row_has_unit = name == "UNIT" and _UNIT_LIKE.search(row.text) is not None
            hit = _inherit_field(page, row, needle, from_header=not row_has_unit)
            if hit is None:
                notes.append(f"{name}_NOT_LOCATED")
            else:
                evidence |= set(page.refs(*hit))
        # A specimen the page never states is discarded by the caller, not trusted; it does not
        # change what was read, so only a missing unit demotes the reading.
        decisive = [note for note in notes if note != "SPECIMEN_NOT_LOCATED"]
        verification = "verified" if not decisive else "unverified reading"
        return LabGrounding(verification, tuple(sorted(evidence)), notes=tuple(notes))
    if label_rows and value_rows:
        anchor = label_rows[0]
        value_row = min(
            value_rows,
            key=lambda row: (
                abs(row.y - anchor.y) if row.y is not None and anchor.y is not None else 1e9
            ),
        )
        alternative, alt_refs = _geometric_label(page, value_row, value_n)
        alt_n = clean(alternative)
        if alt_n and any(character.isalpha() for character in alt_n) and alt_n != label_n:
            evidence = tuple(sorted(page.refs(anchor, label_n)))
            return LabGrounding(
                "conflicting",
                evidence,
                alternative,
                tuple(sorted({*alt_refs, *page.refs(value_row, value_n)})),
                ("ANALYTE_ASSIGNMENT_DISAGREES",),
            )
    return LabGrounding("unverified reading", (), notes=("ASSOCIATION_NOT_LOCATED",))


def uncovered_lab_rows(page: PageText, covered: set[int]) -> list[Row]:
    """Detected laboratory table rows that no structured fact references (D28)."""
    return [row for row in page.rows if looks_like_lab_row(row) and not covered & set(row.indices)]
