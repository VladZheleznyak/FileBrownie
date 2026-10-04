"""Laboratory and specialty histories over the active generation. Deterministic; no model."""

from dataclasses import dataclass, field
from datetime import date, datetime
from uuid import UUID

from filebrownie.evidence.models import TextSpan
from filebrownie.evidence.normalize import contains_phrase, stems
from filebrownie.interpretation.grounding import PageText
from filebrownie.interpretation.dates import DateValue
from filebrownie.query.dictionary import Dictionary, Scope, stem_key
from filebrownie.query.timeline import (
    MAY_FALL,
    OUT,
    UNCERTAIN,
    UNDATED,
    DateRange,
    classify,
    format_alternatives,
)

MAX_LISTED = 30


class QueryError(Exception):
    """Fixed safe codes only."""


@dataclass(frozen=True)
class Row:
    fact_id: UUID
    kind: str  # lab | event
    sort_key: tuple
    date_text: str
    fields: dict[str, str]
    markers: tuple[str, ...]
    sources: str
    other_dates: str
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Candidate:
    row: Row
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class Mention:
    sources: str
    unit: int
    text: str


@dataclass
class HistoryResult:
    kind: str  # labs | visits
    term: str
    scope: Scope
    generation_id: UUID
    scan_finished_at: datetime | None
    dictionary_revision: int
    window: DateRange
    rows: list[Row] = field(default_factory=list)
    undated: list[Row] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    excluded_undated: int = 0
    mentions: list[Mention] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.rows or self.undated or self.candidates)


def source_text(paths: list[str], format: str, unit: int | None = None) -> str:
    where = ""
    if unit is not None:
        where = f" page {unit}" if format == "pdf" else " image"
    first = paths[0] if paths else "unknown source"
    extra = f" (+{len(paths) - 1} identical copies)" if len(paths) > 1 else ""
    return f"{first}{where}{extra}"


def _alternatives(raw: list[dict]) -> tuple[DateValue, ...]:
    return tuple(DateValue.from_json(item) for item in raw)


def _date_text(role: str | None, alternatives: tuple[DateValue, ...], planned: bool) -> str:
    if not alternatives:
        return "-"
    label = "planned" if planned and role == "event" else role
    return f"{format_alternatives(alternatives)} ({label})"


def _other_dates(dates: list[dict], chosen_role: str | None) -> str:
    items = [
        f"{item['role']}{' (planned)' if item['planned'] else ''}: {item['raw']}"
        for item in dates
        if item["role"] != chosen_role
    ]
    return "; ".join(items)


def _reason_for_terminology(status: str) -> str:
    return {
        "inflected": "terminology: inflection-only match, not yet reviewed",
        "auto-mapped": "terminology: auto-mapped, unreviewed",
        "ambiguous": "terminology: ambiguous label, several possible meanings",
    }[status]


def _evidence_keys(row: dict) -> set[tuple[str, str, int, int]]:
    keys = set()
    for field_name in ("evidence", "alternative_evidence"):
        for index in row[field_name]:
            keys.add((row["content_hash"], row["format"], row["unit_number"], index))
    return keys


def _fragment(key: tuple[str, ...]) -> str | None:
    stemmed = [token for token in stem_key(key) if token]
    if not stemmed:
        return None
    longest = max(stemmed, key=len)
    fragment = longest[: max(3, len(longest) - 1)]
    return fragment.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _span_record(record: dict) -> TextSpan:
    bbox = None
    if record["x0"] is not None:
        bbox = (record["x0"], record["y0"], record["x1"], record["y1"])
    return TextSpan(record["text"], bbox, record["reader"])


def sweep(
    repository, generation_id: UUID, scope: Scope, covered: set[tuple[str, str, int, int]]
) -> list[Mention]:
    """Mentions of any synonym in located text that no matching fact's evidence covers (D18)."""
    phrases = [stem_key(key) for key in scope.synonyms]
    fragments = sorted({f for f in (_fragment(key) for key in scope.synonyms) if f})
    units: dict[tuple[str, str, int], list[str]] = {}
    for row in repository.reader.candidate_spans(generation_id, fragments):
        unit = (row["content_hash"], row["format"], row["unit_number"])
        units.setdefault(unit, row["sources"])
    found = []
    for (content_hash, fmt, unit_number), paths in sorted(units.items()):
        records = repository.reader.unit_spans(generation_id, content_hash, fmt, unit_number)
        page = PageText.build(tuple(_span_record(record) for record in records))
        seen: set[tuple[int, str]] = set()
        for phrase in phrases:
            if not phrase:
                continue
            for row in page.rows:
                if not contains_phrase(stems(row.text), phrase):
                    continue
                keys = tuple(
                    (content_hash, fmt, unit_number, index) for index in row.indices
                )
                if all(key in covered for key in keys):
                    continue
                text = " ".join(page.spans[index].text.strip() for index in row.indices)[:100]
                marker = (unit_number, text)
                if marker in seen:
                    continue
                seen.add(marker)
                found.append((paths, fmt, unit_number, text))
    found.sort()
    return [Mention(source_text(paths, fmt, unit), unit, text) for paths, fmt, unit, text in found]


def coverage_warnings(repository, generation_id: UUID, include_tables: bool) -> list[str]:
    reader = repository.reader
    lines: list[str] = []
    for item in reader.unlisted_sources(generation_id):
        extra = f"; {', '.join(item['warnings'])}" if item["warnings"] else ""
        lines.append(f"Not processed: {item['relative_path']} ({item['status']}{extra})")
    for item in reader.file_problems(generation_id):
        units = item["unit_count"]
        lines.append(
            f"File {item['status']}: {source_text(item['sources'], item['format'])} "
            f"(pages known: {item['page_count'] if item['page_count'] is not None else 'unknown'}"
            f"{f'; units recorded: {units}' if units is not None else ''}"
            f"{'; ' + ', '.join(item['warnings']) if item['warnings'] else ''})"
        )
    tables = {
        (row["content_hash"], row["format"], row["unit_number"]): row
        for row in reader.incomplete_table_warnings(generation_id)
    }
    text_layer_files: set[tuple[str, str]] = set()
    for unit in reader.coverage_units(generation_id):
        where = source_text(unit["sources"], unit["format"], unit["unit_number"])
        warnings = set(unit["warnings"])
        file_key = (unit["content_hash"], unit["format"])
        if "TEXT_LAYER_COVERAGE_UNVERIFIED" in warnings:
            warnings.discard("TEXT_LAYER_COVERAGE_UNVERIFIED")
            if file_key not in text_layer_files:
                text_layer_files.add(file_key)
                lines.append(
                    "Text layer does not establish full page coverage: "
                    f"{source_text(unit['sources'], unit['format'])}"
                )
        if unit["status"] != "completed":
            lines.append(
                f"Partially processed: {where} ({unit['status']}; {', '.join(sorted(warnings))})"
            )
        if "HANDWRITING_NOT_INTERPRETED" in warnings:
            lines.append(f"Handwriting not interpreted: {where}")
        if "VISION_ITEMS_DROPPED" in warnings:
            lines.append(f"Vision extraction dropped items: {where}")
        if include_tables and "MISSING_CONTEXT" in warnings:
            lines.append(f"Missing context (date/unit/header): {where}")
        key = (unit["content_hash"], unit["format"], unit["unit_number"])
        if include_tables and key in tables:
            spans = reader.spans(generation_id, *key, [int(i) for i in tables[key]["evidence"]])
            snippet = " | ".join(span["text"].strip() for span in spans)[:120]
            lines.append(
                f"Possible incomplete table extraction: {where}; row without a structured "
                f"result near: {snippet}"
            )
    if len(lines) > MAX_LISTED:
        lines = [*lines[:MAX_LISTED], f"... and {len(lines) - MAX_LISTED} more coverage warnings"]
    return lines


def _lab_row(fact: dict, placement: str, match, verification_markers: list[str]) -> Row:
    alternatives = _alternatives(fact["timeline_alternatives"])
    markers = list(verification_markers)
    if placement == MAY_FALL:
        markers.append("may fall within range")
    if match.status == "auto-mapped":
        markers.append("auto-mapped, unreviewed")
    value = fact["raw_value"]
    fields = {
        "label": fact["raw_label"],
        "value": value,
        "unit": fact["unit"] or "-",
        "reference": fact["reference_interval"] or "-",
        "flag": fact["flag"] or "-",
        "specimen": fact["specimen"] or "-",
    }
    if fact["verification"] == "conflicting":
        fields["label"] = f"{fact['raw_label']} / {fact['alternative_label']}"
    return Row(
        fact["id"],
        "lab",
        (alternatives[0].start if alternatives else None, str(fact["id"])),
        _date_text(fact["timeline_role"], alternatives, False),
        fields,
        tuple(markers),
        source_text(fact["sources"], fact["format"], fact["unit_number"]),
        _other_dates(fact["dates"], fact["timeline_role"]),
        tuple(fact["notes"]),
    )


def _event_row(fact: dict, placement: str, match, verification_markers: list[str]) -> Row:
    alternatives = _alternatives(fact["timeline_alternatives"])
    markers = [*verification_markers, f"evidence: {fact['strength']}"]
    if placement == MAY_FALL:
        markers.append("may fall within range")
    if match.status == "auto-mapped":
        markers.append("auto-mapped, unreviewed")
    event_type = "other — recommendation" if fact["recommendation"] else fact["event_type"]
    if fact["verification"] == "conflicting":
        event_type = f"{event_type} / {fact['alternative_event_type']}"
    fields = {
        "event": event_type.replace("_", " "),
        "specialty": fact["raw_specialty"],
        "wording": fact["wording"][:80],
    }
    return Row(
        fact["id"],
        "event",
        (alternatives[0].start if alternatives else None, str(fact["id"])),
        _date_text(fact["timeline_role"], alternatives, fact["planned"]),
        fields,
        tuple(markers),
        source_text(fact["sources"], fact["format"], fact["unit_number"]),
        _other_dates(fact["dates"], fact["timeline_role"]),
        tuple(fact["notes"]),
    )


def _sorted(rows: list[Row]) -> list[Row]:
    dated = [row for row in rows if row.sort_key[0] is not None]
    undated = [row for row in rows if row.sort_key[0] is None]
    dated.sort(key=lambda row: (row.sort_key[0], row.sources, row.sort_key[1]))
    undated.sort(key=lambda row: (row.sources, row.sort_key[1]))
    return dated + undated


def run_query(
    repository, kind: str, term: str, window: DateRange, dictionary: Dictionary | None = None
) -> HistoryResult:
    """`kind` is `labs` or `visits`. Reads only the active generation (D8)."""
    generation_id = repository.active_generation_id()
    if generation_id is None:
        raise QueryError("NO_ACTIVE_GENERATION")
    generation = next(item for item in repository.generations() if item.id == generation_id)
    if dictionary is None:
        dictionary = repository.dictionary.snapshot(generation_id)
    concept_kind = "analyte" if kind == "labs" else "specialty"
    scope = dictionary.resolve(term, concept_kind)
    result = HistoryResult(
        kind, term, scope, generation_id, generation.finished_at, dictionary.revision, window
    )
    if kind == "labs":
        facts, label_field, build_row = (
            repository.reader.lab_results(generation_id),
            "raw_label",
            _lab_row,
        )
        alternative_field = "alternative_label"
    else:
        facts, label_field, build_row = (
            repository.reader.specialty_events(generation_id),
            "raw_specialty",
            _event_row,
        )
        alternative_field = None
    covered: set[tuple[str, str, int, int]] = set()
    rows: list[Row] = []
    for fact in facts:
        match = dictionary.match(fact[label_field], scope)
        if match is None and alternative_field and fact[alternative_field]:
            match = dictionary.match(fact[alternative_field], scope)
        if match is None:
            continue
        covered |= _evidence_keys(fact)
        timeline = _alternatives(fact["timeline_alternatives"])
        placement = classify(timeline, window)
        if placement == OUT:
            continue
        if placement == UNDATED and window.active:
            result.excluded_undated += 1
            continue
        reasons = []
        markers = []
        if not match.confirmed:
            reasons.append(_reason_for_terminology(match.status))
        if fact["verification"] == "conflicting":
            other = fact["alternative_label"] or fact["alternative_event_type"]
            reasons.append(f"unresolved reading: readers disagree; alternative: {other}")
        elif fact["verification"] == "unverified reading":
            markers.append("unverified reading")
        if placement == UNCERTAIN:
            reasons.append(f"date uncertain: {format_alternatives(timeline)}")
        row = build_row(fact, placement, match, markers)
        if reasons:
            result.candidates.append(Candidate(row, tuple(reasons)))
        else:
            rows.append(row)
    ordered = _sorted(rows)
    result.rows = [row for row in ordered if row.sort_key[0] is not None]
    result.undated = [row for row in ordered if row.sort_key[0] is None]
    result.candidates.sort(
        key=lambda item: (
            item.row.sort_key[0] is None,
            item.row.sort_key[0] or date.min,
            item.row.sources,
        )
    )
    result.mentions = sweep(repository, generation_id, scope, covered)
    result.warnings = coverage_warnings(repository, generation_id, include_tables=kind == "labs")
    if kind == "labs":
        units: dict[str, int] = {}
        for row in [*result.rows, *result.undated, *(c.row for c in result.candidates)]:
            units[row.fields["unit"]] = units.get(row.fields["unit"], 0) + 1
        if len(units) > 1:
            listing = ", ".join(f"{name} ({count})" for name, count in sorted(units.items()))
            result.notes.append(
                f"Different units are reported: {listing}. Values are shown as written."
            )
    if scope.concepts or scope.group:
        involved = sorted(scope.concepts)
        result.notes.append(
            "Matched concepts: " + ", ".join(dictionary.describe(item) for item in involved)
        )
    else:
        result.notes.append("Term is not in the dictionary; matching raw labels only.")
    return result
