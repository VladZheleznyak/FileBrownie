"""Turn a page's untrusted vision claims plus located text into verified/unverified facts."""

import re
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation

from filebrownie.evidence.models import TextSpan
from filebrownie.evidence.normalize import contains_token, fold_signs, normalize
from filebrownie.interpretation.dates import DateValue, parse_date
from filebrownie.interpretation.grounding import (
    PageText,
    Row,
    clean,
    ground_lab_row,
    looks_like_lab_row,
    uncovered_lab_rows,
)
from filebrownie.interpretation.models import (
    CONFLICTING,
    UNVERIFIED,
    VERIFIED,
    EventFact,
    InterpretationWarning,
    LabFact,
    ReportedDate,
    Timeline,
    UnitInterpretation,
)
from filebrownie.interpretation.vision import VisionDate, VisionEvent, VisionLabRow, VisionPage

LAB_DATE_PREFERENCE = ("specimen", "report", "unspecified")
EVENT_DATE_PREFERENCE = ("event",)
_VALUE = re.compile(r"^(?P<cmp><=|>=|<|>|≤|≥)?(?P<sign>[+-])?(?P<num>\d+(?:[.,]\d+)?)$")


def parse_value(raw: str) -> tuple[str | None, str | None, bool]:
    """Return (comparator, parsed number, qualitative). The raw string is always kept."""
    text = fold_signs(raw.strip().replace(" ", ""))
    match = _VALUE.match(text)
    if match:
        number = match["num"].replace(",", ".")
        if match["sign"]:
            number = f"{match['sign']}{number}"
        try:
            Decimal(number)
        except InvalidOperation:  # pragma: no cover - guarded by the regex
            return None, None, False
        return match["cmp"], number, False
    return None, None, not any(character.isdigit() for character in raw)


_SPECIMEN_CUES = (
    "specimen",
    "collected",
    "collection",
    "drawn",
    "забор",
    "взят",
    "взяті",
    "зразок",
)
_REPORT_CUES = ("report", "issued", "received", "printed", "reported", "видано", "видан", "результат")
_NEGATION = re.compile(
    r"(?:^|\s)(?:no|not|never|denied|without|neither|nor|не|нет|ні|ни|без|відсут)(?:\s|$)",
    re.IGNORECASE,
)
_CANCELLATION = re.compile(
    r"(?:cancel|postpon|reschedul|no[\s-]?show|did\s+not\s+attend|"
    r"отмен|скасов|перенес|не\s+состоял|не\s+відбул|не\s+явил|не\s+з'явив)",
    re.IGNORECASE,
)
_CLAUSE_SPLIT = re.compile(r"[.;!?\n]+")
_NON_TIMELINE_DATE_CAPTION = re.compile(
    r"\b(?:date\s+of\s+birth|dob|born|birthday|"
    r"дата\s+рожд|рожд(?:ения|енн)|"
    r"дата\s+народж)\b",
    re.IGNORECASE,
)
# A numeric date already written on a caption line. Hyphenated ranges such as 10-20 are not dates.
_STATED_DATE = re.compile(r"\d{1,2}[./]\d{1,2}[./]\d{2,4}|\d{4}-\d{2}-\d{2}")
_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)


def _date_row(page: PageText, evidence: tuple[int, ...]) -> Row | None:
    if not evidence:
        return None
    index = evidence[0]
    for row in page.rows:
        if index in row.indices:
            return row
    return None


def _date_context_text(page: PageText, evidence: tuple[int, ...]) -> str:
    """Caption rows above the located date plus the date row (not laboratory data rows)."""
    row = _date_row(page, evidence)
    if row is None:
        return ""
    segments: list[str] = []
    for header in reversed(page.above(row)):
        if looks_like_lab_row(header):
            break
        segments.append(header.text)
    segments.append(row.text)
    return " ".join(segments)


def _row_has_other_caption(row_text: str, date_raw: str) -> bool:
    """True when the date line has words of its own, such as ``Printed:``."""
    date_n = clean(date_raw)
    remaining = row_text.replace(date_n, " ", 1) if date_n else row_text
    return _LETTER.search(remaining) is not None


def _header_states_other_date(header_text: str, date_raw: str) -> bool:
    """True when the header already states a different date, so its role belongs to that date."""
    date_n = clean(date_raw)
    return any(clean(match.group(0)) != date_n for match in _STATED_DATE.finditer(header_text))


def _date_role_context_text(page: PageText, evidence: tuple[int, ...], date_raw: str) -> str:
    """Caption text used for lab date roles.

    A pure date line may use the caption row above it. A line that already has its own words,
    and a header that already states another date, do not lend their role to this date.
    """
    row = _date_row(page, evidence)
    if row is None:
        return ""
    if _NON_TIMELINE_DATE_CAPTION.search(row.text) or _row_has_other_caption(row.text, date_raw):
        return row.text
    above = page.above(row)
    if not above or looks_like_lab_row(above[0]):
        return row.text
    header = above[0]
    blocked = _NON_TIMELINE_DATE_CAPTION.search(header.text) or _header_states_other_date(
        header.text, date_raw
    )
    if blocked:
        return row.text
    return f"{header.text} {row.text}"


def _date_x_center(page: PageText, evidence: tuple[int, ...]) -> float | None:
    positions = []
    for index in evidence:
        bbox = page.spans[index].bbox
        if bbox is not None:
            positions.append((bbox[0] + bbox[2]) / 2)
    if not positions:
        return None
    return sum(positions) / len(positions)


def _column_aligned_date_role(
    page: PageText, evidence: tuple[int, ...], date_raw: str
) -> str | None:
    """Match a date under a two-column caption by horizontal alignment."""
    row = _date_row(page, evidence)
    if row is None or _nearest_lab_date_role_before(row.text, date_raw):
        return None
    above = page.above(row)
    if not above or looks_like_lab_row(above[0]):
        return None
    header = above[0]
    date_x = _date_x_center(page, evidence)
    if date_x is None:
        return None
    best_role: str | None = None
    best_distance = float("inf")
    for role, cues in (("specimen", _SPECIMEN_CUES), ("report", _REPORT_CUES)):
        for index in header.indices:
            text = page.normalized[index]
            if not any(cue in text for cue in cues):
                continue
            bbox = page.spans[index].bbox
            if bbox is None:
                continue
            cue_x = (bbox[0] + bbox[2]) / 2
            distance = abs(cue_x - date_x)
            if distance < best_distance:
                best_distance = distance
                best_role = role
    return best_role


def _nearest_lab_date_role_before(row_text: str, date_raw: str) -> str | None:
    """Role cue closest to the left of the located date in the caption context, if any."""
    date_n = clean(date_raw)
    if not date_n:
        return None
    start = row_text.find(date_n)
    if start < 0:
        return None
    prefix = row_text[:start]
    best_end = -1
    best_role: str | None = None
    for role, cues in (("specimen", _SPECIMEN_CUES), ("report", _REPORT_CUES)):
        for cue in cues:
            pos = prefix.rfind(cue)
            if pos < 0:
                continue
            cue_end = pos + len(cue)
            if cue_end > best_end:
                best_end = cue_end
                best_role = role
    return best_role


def _supported_date_role(page: PageText, claim: VisionDate, evidence: tuple[int, ...]) -> str:
    """Return the role when located text supports it; otherwise `unsupported` or `unspecified`."""
    row = _date_row(page, evidence)
    if row is None:
        return "unsupported"
    if claim.role in ("specimen", "report"):
        on_row = _nearest_lab_date_role_before(row.text, claim.raw)
        if on_row is not None:
            return claim.role if on_row == claim.role else "unsupported"
        aligned = _column_aligned_date_role(page, evidence, claim.raw)
        if aligned is not None:
            return claim.role if aligned == claim.role else "unsupported"
        text = _date_role_context_text(page, evidence, claim.raw)
        nearest = _nearest_lab_date_role_before(text, claim.raw)
        if nearest != claim.role:
            return "unsupported"
        return claim.role
    text = _date_role_context_text(page, evidence, claim.raw)
    if claim.role == "event":
        if _nearest_lab_date_role_before(text, claim.raw) is not None:
            return "unsupported"
        return "event"
    if claim.role == "unspecified":
        return "unspecified"
    return "unsupported"


def _date_near_wording(page: PageText, wording_n: str, evidence: tuple[int, ...]) -> bool:
    if not wording_n:
        return False
    date_row = _date_row(page, evidence)
    if date_row is None:
        return False
    wording_rows = page.rows_with(wording_n)
    for wording_row in wording_rows:
        if wording_row == date_row:
            return True
        if (
            wording_row.y is not None
            and date_row.y is not None
            and abs(wording_row.y - date_row.y) <= 3 * max(wording_row.height, date_row.height)
        ):
            return True
    return False


def _dates(
    page: PageText,
    claims: Sequence[VisionDate],
    planned: bool = False,
    event_wording: str | None = None,
) -> list[ReportedDate]:
    reported: list[ReportedDate] = []
    wording_n = clean(event_wording) if event_wording else ""
    for claim in claims:
        evidence = page.locate(claim.raw)
        supported_role = _supported_date_role(page, claim, evidence) if evidence else "unsupported"
        role_supported = supported_role != "unsupported"
        display_role = claim.role if not role_supported else supported_role
        if role_supported and claim.role == "event" and wording_n:
            if not _date_near_wording(page, wording_n, evidence):
                role_supported = False
        if not evidence:
            reported.append(
                ReportedDate(
                    claim.raw,
                    claim.role,
                    parse_date(claim.raw),
                    (),
                    planned,
                    role_supported=False,
                )
            )
            continue
        reported.append(
            ReportedDate(
                claim.raw,
                display_role,
                parse_date(claim.raw),
                evidence,
                planned,
                role_supported=role_supported,
            )
        )
    return reported


def build_timeline(dates: Sequence[ReportedDate], preference: Sequence[str]) -> Timeline:
    """Use the highest-priority reported role. Ambiguity is kept, never resolved (D34)."""
    for role in preference:
        chosen = [
            item
            for item in dates
            if item.grounded and item.role_supported and item.role == role and item.alternatives
        ]
        if not chosen:
            continue
        alternatives: list[DateValue] = []
        for item in chosen:
            alternatives.extend(item.alternatives)
        return Timeline(role, tuple(dict.fromkeys(alternatives)))
    return Timeline(None, ())


def _lab_dates(row: VisionLabRow, page_dates: Sequence[VisionDate]) -> list[VisionDate]:
    own_roles = {claim.role for claim in row.dates}
    return [*row.dates, *(claim for claim in page_dates if claim.role not in own_roles)]


def _shared_result_row(page: PageText, label: str, value: str) -> Row | None:
    label_n, value_n = clean(label), clean(value)
    shared = [row for row in page.rows_with(label_n) if row in page.rows_with(value_n)]
    return shared[0] if shared else None


def _lab_fact(page: PageText, vision: VisionPage, row: VisionLabRow) -> LabFact:
    grounding = ground_lab_row(page, row.label, row.value, row.unit, row.specimen)
    dates = _dates(page, _lab_dates(row, vision.dates))
    notes = list(grounding.notes)
    verification = grounding.verification
    evidence = set(grounding.evidence)
    for item in dates:
        if not item.grounded:
            notes.append("DATE_NOT_LOCATED")
            if verification == VERIFIED:
                verification = UNVERIFIED
            continue
        evidence |= set(item.evidence)
        if not item.role_supported:
            notes.append("DATE_ROLE_UNSUPPORTED")
            if verification == VERIFIED:
                verification = UNVERIFIED
    if verification == VERIFIED and vision.context_missing:
        verification = UNVERIFIED
        notes.append("MISSING_CONTEXT")
    timeline = build_timeline(dates, LAB_DATE_PREFERENCE)
    if timeline.role is not None and not timeline.alternatives:
        notes.append("DATE_UNPARSEABLE")
    result_row = _shared_result_row(page, row.label, row.value)
    reference_interval = row.reference_interval
    flag = row.flag
    if reference_interval:
        needle = clean(reference_interval)
        if result_row is None or not needle or not contains_token(result_row.text, needle):
            notes.append("REFERENCE_NOT_LOCATED")
            reference_interval = None
            if verification == VERIFIED:
                verification = UNVERIFIED
    if flag:
        needle = clean(flag)
        if result_row is None or not needle or not contains_token(result_row.text, needle):
            notes.append("FLAG_NOT_LOCATED")
            flag = None
            if verification == VERIFIED:
                verification = UNVERIFIED
    comparator, number, qualitative = parse_value(row.value)
    return LabFact(
        row.label,
        row.value,
        comparator,
        number,
        qualitative,
        row.unit,
        reference_interval,
        flag,
        None if "SPECIMEN_NOT_LOCATED" in grounding.notes else row.specimen,
        tuple(dates),
        timeline,
        verification,
        tuple(sorted(evidence)),
        grounding.alternative_label,
        grounding.alternative_evidence,
        tuple(dict.fromkeys(notes)),
    )


# Wording cues, matched on normalized text. Referral/appointment/recommendation cues outrank
# the object words ("referral for a consultation" is not an encounter).
_CUES: dict[str, tuple[str, ...]] = {
    "recommendation": ("рекоменд", "recommend", "порад"),
    "referral": ("направлен", "направля", "referral", "referred", "refer to", "направл"),
    "appointment_scheduled": ("запис", "scheduled", "booked", "заплан", "призначен", "назначен"),
    "appointment_confirmed": ("подтвержд", "підтверд", "confirmed"),
    "encounter": (
        "консультац",
        "consultation",
        "прием",
        "приём",
        "прийом",
        "осмотр",
        "огляд",
        "visit",
        "examin",
    ),
    "procedure": ("процедур", "операци", "біопс", "биопс", "procedure", "biopsy", "surgery"),
    "result": ("заключен", "висновок", "result", "ultrasound", "узи", "узд", "mri", "мрт"),
    "discharge": ("выписк", "виписк", "discharge"),
    "invoice": ("счет", "рахунок", "invoice", "receipt", "bill"),
}


def _clauses(text: str) -> tuple[str, ...]:
    parts = [clause.strip() for clause in _CLAUSE_SPLIT.split(text) if clause.strip()]
    return tuple(parts) if parts else ((text,) if text else ())


def _types_in_clause(clause: str) -> set[str]:
    return {name for name, cues in _CUES.items() if any(cue in clause for cue in cues)}


def _clause_blocks_event(clause: str) -> bool:
    return _NEGATION.search(clause) is not None or _CANCELLATION.search(clause) is not None


def wording_types(wording: str) -> set[str]:
    """Event types the wording supports.

    A clause that negates or cancels its own cue withdraws that type. A later bare
    cancellation withdraws types already found. A later positive clause can state the
    event again. Negation of an unrelated clause, such as a symptom, leaves earlier
    event types in place.
    """
    found: set[str] = set()
    for clause in _clauses(normalize(wording)):
        present = _types_in_clause(clause)
        if _clause_blocks_event(clause):
            if present:
                found -= present
            elif _CANCELLATION.search(clause):
                found.clear()
            continue
        found |= present
    if "recommendation" in found:
        return {"other"}
    if found & {"referral", "appointment_scheduled", "appointment_confirmed"}:
        found -= {"encounter", "procedure", "result"}
    return found


_DIRECT_CLASSES = {
    "referral": {"referral"},
    "appointment_scheduled": {"appointment"},
    "appointment_confirmed": {"appointment"},
    "encounter": {"visit_note"},
    "procedure": {"visit_note", "discharge"},
    "result": {"imaging_report", "lab_report", "visit_note"},
    "discharge": {"discharge"},
    "invoice": {"invoice"},
    "other": {"visit_note", "discharge", "referral"},
}


def evidence_strength(
    event: VisionEvent, document_class: str, handwriting: bool, verified: bool
) -> str:
    """direct / indirect / weak per D41; separate from source verification."""
    if handwriting:
        return "weak"
    if not verified:
        return "indirect"
    expected = wording_types(event.wording) if clean(event.wording) else set()
    if expected and event.event_type not in expected:
        return "indirect"
    return (
        "direct" if document_class in _DIRECT_CLASSES.get(event.event_type, set()) else "indirect"
    )


def _event_fact(page: PageText, vision: VisionPage, event: VisionEvent) -> EventFact | None:
    specialty_n, wording_n = clean(event.specialty), clean(event.wording)
    specialty_rows, wording_rows = page.rows_with(specialty_n), page.rows_with(wording_n)
    notes: list[str] = []
    verification, evidence = UNVERIFIED, set()
    near = [
        (s, w)
        for s in specialty_rows
        for w in wording_rows
        if s == w
        or (s.y is not None and w.y is not None and abs(s.y - w.y) <= 3 * max(s.height, w.height))
    ]
    expected = wording_types(event.wording) if wording_n else set()
    if not expected:
        return EventFact(
            event.specialty,
            event.event_type,
            event.recommendation,
            event.wording,
            event.planned,
            "indirect",
            (),
            Timeline(None, ()),
            UNVERIFIED,
            (),
            None,
            (),
            ("EVENT_WORDING_INSUFFICIENT",),
        )
    if near:
        s, w = near[0]
        source_text = s.text if s == w else f"{s.text} {w.text}"
        expected = wording_types(source_text)
        if not expected:
            return None
        evidence = set(page.refs(s, specialty_n)) | set(page.refs(w, wording_n))
        verification = VERIFIED
    else:
        notes.append("EVENT_NOT_GROUNDED" if wording_n else "EVENT_WORDING_MISSING")
    dates = _dates(page, event.dates, planned=event.planned, event_wording=event.wording)
    for item in dates:
        if not item.grounded:
            notes.append("DATE_NOT_LOCATED")
            if verification == VERIFIED:
                verification = UNVERIFIED
            continue
        evidence |= set(item.evidence)
        if not item.role_supported:
            notes.append("DATE_ROLE_UNSUPPORTED")
            if verification == VERIFIED:
                verification = UNVERIFIED
    alternative, alternative_evidence = None, ()
    if verification == VERIFIED and expected and event.event_type not in expected:
        verification = CONFLICTING
        alternative = sorted(expected)[0]
        alternative_evidence = tuple(sorted(evidence))
        notes.append("EVENT_TYPE_DISAGREES")
    own = [
        item
        for item in dates
        if item.grounded and item.role_supported and item.role == "event" and item.alternatives
    ]
    timeline = build_timeline(own, EVENT_DATE_PREFERENCE)
    if timeline.role is not None and not timeline.alternatives:
        notes.append("DATE_UNPARSEABLE")
    return EventFact(
        event.specialty,
        event.event_type,
        event.recommendation,
        event.wording,
        event.planned,
        evidence_strength(
            event, vision.document_class, vision.handwriting, verification == VERIFIED
        ),
        tuple(dates),
        timeline,
        verification,
        tuple(sorted(evidence)),
        alternative,
        alternative_evidence,
        tuple(dict.fromkeys(notes)),
    )


def interpret_page(spans: Sequence[TextSpan], vision: VisionPage) -> UnitInterpretation:
    page = PageText.build(spans)
    lab_facts = tuple(_lab_fact(page, vision, row) for row in vision.lab_rows)
    # Events without source wording, or mention-only wording, stay out of visit rows (D36).
    events = tuple(
        fact
        for event in vision.events
        if clean(event.wording) and wording_types(event.wording)
        for fact in (_event_fact(page, vision, event),)
        if fact is not None
    )
    warnings = []
    if vision.handwriting:
        warnings.append(InterpretationWarning("HANDWRITING_NOT_INTERPRETED"))
    if vision.context_missing:
        warnings.append(InterpretationWarning("MISSING_CONTEXT"))
    if vision.dropped:
        warnings.append(InterpretationWarning("VISION_ITEMS_DROPPED"))
    covered = {
        index for fact in lab_facts for index in (*fact.evidence, *fact.alternative_evidence)
    }
    for row in uncovered_lab_rows(page, covered):
        warnings.append(InterpretationWarning("POSSIBLE_INCOMPLETE_TABLE_EXTRACTION", row.indices))
    return UnitInterpretation(vision.document_class, lab_facts, events, tuple(warnings))
