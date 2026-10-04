"""Turn a page's untrusted vision claims plus located text into verified/unverified facts."""

import re
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation

from filebrownie.evidence.models import TextSpan
from filebrownie.evidence.normalize import normalize
from filebrownie.interpretation.dates import DateValue, parse_date
from filebrownie.interpretation.grounding import (
    PageText,
    clean,
    ground_lab_row,
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
_VALUE = re.compile(r"^(?P<cmp><=|>=|<|>|≤|≥)?\s*(?P<num>\d+(?:[.,]\d+)?)$")


def parse_value(raw: str) -> tuple[str | None, str | None, bool]:
    """Return (comparator, parsed number, qualitative). The raw string is always kept."""
    text = raw.strip().replace(" ", "")
    match = _VALUE.match(text)
    if match:
        number = match["num"].replace(",", ".")
        try:
            Decimal(number)
        except InvalidOperation:  # pragma: no cover - guarded by the regex
            return None, None, False
        return match["cmp"], number, False
    return None, None, not any(character.isdigit() for character in raw)


def _dates(
    page: PageText, claims: Sequence[VisionDate], planned: bool = False
) -> list[ReportedDate]:
    return [
        ReportedDate(claim.raw, claim.role, parse_date(claim.raw), page.locate(claim.raw), planned)
        for claim in claims
    ]


def build_timeline(dates: Sequence[ReportedDate], preference: Sequence[str]) -> Timeline:
    """Use the highest-priority reported role. Ambiguity is kept, never resolved (D34)."""
    for role in preference:
        chosen = [item for item in dates if item.role == role]
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


def _lab_fact(page: PageText, vision: VisionPage, row: VisionLabRow) -> LabFact:
    grounding = ground_lab_row(page, row.label, row.value, row.unit, row.specimen)
    dates = _dates(page, _lab_dates(row, vision.dates))
    notes = list(grounding.notes)
    verification = grounding.verification
    evidence = set(grounding.evidence)
    for item in dates:
        if item.grounded:
            evidence |= set(item.evidence)
        else:
            notes.append("DATE_NOT_LOCATED")
            if verification == VERIFIED:
                verification = UNVERIFIED
    if verification == VERIFIED and vision.context_missing:
        verification = UNVERIFIED
        notes.append("MISSING_CONTEXT")
    timeline = build_timeline(dates, LAB_DATE_PREFERENCE)
    if timeline.role is not None and not timeline.alternatives:
        notes.append("DATE_UNPARSEABLE")
    comparator, number, qualitative = parse_value(row.value)
    return LabFact(
        row.label,
        row.value,
        comparator,
        number,
        qualitative,
        row.unit,
        row.reference_interval,
        row.flag,
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


def wording_types(wording: str) -> set[str]:
    text = normalize(wording)
    found = {name for name, cues in _CUES.items() if any(cue in text for cue in cues)}
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


def evidence_strength(event: VisionEvent, document_class: str, handwriting: bool) -> str:
    """direct / indirect / weak per D41; separate from source verification."""
    if handwriting:
        return "weak"
    return (
        "direct" if document_class in _DIRECT_CLASSES.get(event.event_type, set()) else "indirect"
    )


def _event_fact(page: PageText, vision: VisionPage, event: VisionEvent) -> EventFact:
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
    if near:
        s, w = near[0]
        evidence = set(page.refs(s, specialty_n)) | set(page.refs(w, wording_n))
        verification = VERIFIED
    else:
        notes.append("EVENT_NOT_GROUNDED" if wording_n else "EVENT_WORDING_MISSING")
    dates = _dates(page, event.dates, planned=event.planned)
    for item in dates:
        if item.grounded:
            evidence |= set(item.evidence)
        else:
            notes.append("DATE_NOT_LOCATED")
            if verification == VERIFIED:
                verification = UNVERIFIED
    alternative, alternative_evidence = None, ()
    expected = wording_types(event.wording) if wording_n else set()
    if verification == VERIFIED and expected and event.event_type not in expected:
        verification = CONFLICTING
        alternative = sorted(expected)[0]
        alternative_evidence = tuple(sorted(evidence))
        notes.append("EVENT_TYPE_DISAGREES")
    own = [item for item in dates if item.role == "event"]
    timeline = build_timeline(own, EVENT_DATE_PREFERENCE)
    if timeline.role is not None and not timeline.alternatives:
        notes.append("DATE_UNPARSEABLE")
    return EventFact(
        event.specialty,
        event.event_type,
        event.recommendation,
        event.wording,
        event.planned,
        evidence_strength(event, vision.document_class, vision.handwriting),
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
    # Events without source wording are mention-only evidence, never rows (D36).
    events = tuple(
        _event_fact(page, vision, event) for event in vision.events if clean(event.wording)
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
