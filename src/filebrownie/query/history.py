"""Laboratory and specialty histories over the active generation. Deterministic; no model."""

from dataclasses import dataclass, field, replace
from datetime import date, datetime
from uuid import UUID

from filebrownie.evidence.models import TextSpan
from filebrownie.evidence.normalize import contains_phrase, stems
from filebrownie.interpretation.dates import DateValue
from filebrownie.interpretation.grounding import PageText, clean
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


def _other_dates(
    dates: list[dict], chosen_role: str | None, exclude_raw: tuple[str, ...] = ()
) -> str:
    items = []
    for item in dates:
        if item["role"] == chosen_role:
            continue
        if item["raw"] in exclude_raw:
            continue
        if not item.get("alternatives"):
            continue
        role = item["role"]
        if not item.get("role_supported", True):
            role = f"{role} (unsupported)"
        suffix = " (planned)" if item["planned"] else ""
        items.append(f"{role}{suffix}: {item['raw']}")
    return "; ".join(items)


def _path_from_source_label(sources: str) -> str:
    for marker in (" page ", " image"):
        if marker in sources:
            return sources.split(marker, 1)[0]
    if " (+" in sources:
        return sources.split(" (+", 1)[0]
    return sources


def _located_unconfirmed_lab_dates(
    fact: dict,
) -> tuple[str | None, tuple[DateValue, ...], str | None]:
    if fact["timeline_role"] is not None and fact["timeline_alternatives"]:
        return None, (), None
    if "DATE_ROLE_UNSUPPORTED" not in fact["notes"]:
        return None, (), None
    for role in ("specimen", "report", "unspecified"):
        for item in fact["dates"]:
            if item["role"] != role:
                continue
            if item.get("role_supported", True):
                continue
            if not item.get("evidence"):
                continue
            alternatives = _alternatives(item.get("alternatives") or [])
            if alternatives:
                return role, alternatives, item["raw"]
    return None, (), None


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


def _newer_staged_scan(repository, active_id: UUID, active_started_at: datetime):
    newer = [
        item
        for item in repository.generations()
        if item.id != active_id
        and item.kind == "scan"
        and item.state == "staged"
        and item.finished_at is not None
        and item.started_at > active_started_at
    ]
    return newer[-1] if newer else None


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
        row_count = len(page.rows)
        for phrase in phrases:
            if not phrase:
                continue
            for start in range(row_count):
                for end in range(start, min(start + 3, row_count)):
                    sequence = page.rows[start : end + 1]
                    haystack = tuple(
                        token for row in sequence for token in stems(row.text)
                    )
                    if not contains_phrase(haystack, phrase):
                        continue
                    refs: set[int] = set()
                    for row in sequence:
                        for token in phrase:
                            needle = clean(token)
                            if needle:
                                refs.update(page.refs(row, needle))
                    if not refs:
                        for row in sequence:
                            refs.update(row.indices)
                    keys = tuple(
                        (content_hash, fmt, unit_number, index) for index in sorted(refs)
                    )
                    if keys and all(key in covered for key in keys):
                        continue
                    text = " ".join(
                        page.spans[index].text.strip()
                        for row in sequence
                        for index in row.indices
                    )[:100]
                    marker = (unit_number, text)
                    if marker in seen:
                        continue
                    seen.add(marker)
                    found.append((paths, fmt, unit_number, text))
    found.sort()
    mentions: list[Mention] = []
    index = 0
    while index < len(found):
        paths, fmt, unit_number, first = found[index]
        texts = [first]
        index += 1
        while index < len(found) and found[index][:3] == (paths, fmt, unit_number):
            texts.append(found[index][3])
            index += 1
        if len(texts) == 1:
            body = texts[0]
        else:
            body = f"{len(texts)} unmatched mentions on this page; e.g. {texts[0]}"
        mentions.append(Mention(source_text(paths, fmt, unit_number), unit_number, body))
    return mentions


def coverage_warnings(
    repository,
    generation_id: UUID,
    include_tables: bool,
    priority_paths: frozenset[str] | None = None,
) -> list[str]:
    reader = repository.reader
    tagged: list[tuple[bool, str]] = []

    def tag(line: str, paths: list[str]) -> None:
        if priority_paths is None:
            tagged.append((True, line))
            return
        tagged.append((any(path in priority_paths for path in paths), line))

    unlisted = list(reader.unlisted_sources(generation_id))
    if priority_paths is None:
        for item in unlisted:
            extra = f"; {', '.join(item['warnings'])}" if item["warnings"] else ""
            tag(f"Not processed: {item['relative_path']} ({item['status']}{extra})", [])
    elif unlisted:
        tag(f"Not processed: {len(unlisted)} unsupported files (see scan inventory).", [])

    for item in reader.file_problems(generation_id):
        units = item["unit_count"]
        tag(
            f"File {item['status']}: {source_text(item['sources'], item['format'])} "
            f"(pages known: {item['page_count'] if item['page_count'] is not None else 'unknown'}"
            f"{f'; units recorded: {units}' if units is not None else ''}"
            f"{'; ' + ', '.join(item['warnings']) if item['warnings'] else ''})",
            item["sources"],
        )
    tables = {
        (row["content_hash"], row["format"], row["unit_number"]): row
        for row in reader.incomplete_table_warnings(generation_id)
    }
    text_layer_files: set[tuple[str, str]] = set()
    for unit in reader.coverage_units(generation_id):
        paths = unit["sources"]
        where = source_text(paths, unit["format"], unit["unit_number"])
        warnings = set(unit["warnings"])
        file_key = (unit["content_hash"], unit["format"])
        if "TEXT_LAYER_COVERAGE_UNVERIFIED" in warnings:
            warnings.discard("TEXT_LAYER_COVERAGE_UNVERIFIED")
            if file_key not in text_layer_files:
                text_layer_files.add(file_key)
                tag(
                    "Text layer does not establish full page coverage: "
                    f"{source_text(paths, unit['format'])}",
                    paths,
                )
        if unit["status"] != "completed":
            tag(
                f"Partially processed: {where} ({unit['status']}; {', '.join(sorted(warnings))})",
                paths,
            )
        if "HANDWRITING_NOT_INTERPRETED" in warnings:
            tag(f"Handwriting not interpreted: {where}", paths)
        if "VISION_ITEMS_DROPPED" in warnings:
            tag(f"Vision extraction dropped items: {where}", paths)
        if include_tables and "MISSING_CONTEXT" in warnings:
            tag(f"Missing context (date/unit/header): {where}", paths)
        key = (unit["content_hash"], unit["format"], unit["unit_number"])
        if include_tables and key in tables:
            spans = reader.spans(generation_id, *key, [int(i) for i in tables[key]["evidence"]])
            snippet = " | ".join(span["text"].strip() for span in spans)[:120]
            tag(
                f"Possible incomplete table extraction: {where}; row without a structured "
                f"result near: {snippet}",
                paths,
            )

    priority_lines = [line for is_priority, line in tagged if is_priority]
    other_lines = [line for is_priority, line in tagged if not is_priority]
    if priority_paths is None:
        lines = priority_lines + other_lines
    else:
        lines = priority_lines[:MAX_LISTED]
        if len(lines) < MAX_LISTED:
            lines.extend(other_lines[: MAX_LISTED - len(lines)])
    total = len(priority_lines) + len(other_lines)
    hidden = total - len(lines)
    if hidden:
        if priority_paths is None:
            lines.append(f"... and {hidden} more coverage warnings")
        else:
            lines.append(f"... and {hidden} more coverage warnings elsewhere in the collection")
    return lines


def _lab_row(fact: dict, placement: str, match, verification_markers: list[str]) -> Row:
    alternatives = _alternatives(fact["timeline_alternatives"])
    markers = list(verification_markers)
    if placement == MAY_FALL:
        markers.append("may fall within range")
    if match.status == "auto-mapped":
        markers.append("auto-mapped, unreviewed")
    if fact["timeline_role"] == "filename" or "FILENAME_DATE_INFERRED" in fact["notes"]:
        markers.append("inferred from filename")
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
    staged = _newer_staged_scan(repository, generation_id, generation.started_at)
    if staged is not None:
        finished = staged.finished_at.isoformat() if staged.finished_at else "unknown"
        result.notes.append(
            f"A newer scan ({str(staged.id)[:8]}, finished {finished}) is staged and is not "
            "included until you activate it."
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
    priority_paths: set[str] = set()
    for fact in facts:
        match = dictionary.match(fact[label_field], scope)
        if match is None and alternative_field and fact[alternative_field]:
            match = dictionary.match(fact[alternative_field], scope)
        if match is None:
            continue
        covered |= _evidence_keys(fact)
        unconfirmed_role, unconfirmed_alts, unconfirmed_raw = (
            _located_unconfirmed_lab_dates(fact) if kind == "labs" else (None, (), None)
        )
        timeline = _alternatives(fact["timeline_alternatives"])
        display_timeline = unconfirmed_alts if unconfirmed_alts else timeline
        placement = classify(display_timeline, window)
        if placement == OUT:
            continue
        if placement == UNDATED and window.active and not unconfirmed_alts:
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
        if kind == "labs" and "ASSOCIATION_NOT_LOCATED" in fact["notes"]:
            reasons.append("reading not located in source")
        if unconfirmed_alts:
            reasons.append("date role not confirmed")
        if placement == UNCERTAIN:
            reasons.append(f"date uncertain: {format_alternatives(display_timeline)}")
        row = build_row(fact, placement, match, markers)
        if unconfirmed_alts and unconfirmed_role is not None and unconfirmed_raw is not None:
            row = replace(
                row,
                date_text=_date_text(unconfirmed_role, unconfirmed_alts, False),
                sort_key=(unconfirmed_alts[0].start, str(fact["id"])),
                other_dates=_other_dates(
                    fact["dates"], fact["timeline_role"], (unconfirmed_raw,)
                ),
            )
        row_source = _path_from_source_label(row.sources)
        priority_paths.add(row_source)
        for mention_path in fact["sources"]:
            priority_paths.add(mention_path)
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
    for mention in result.mentions:
        priority_paths.add(_path_from_source_label(mention.sources))
    result.warnings = coverage_warnings(
        repository,
        generation_id,
        include_tables=kind == "labs",
        priority_paths=frozenset(priority_paths),
    )
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
