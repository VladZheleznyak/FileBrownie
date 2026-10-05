"""Explicit N1–N3 probes from docs/mvp-state-review-2026-10-04-recheck-2.md."""

from support import row

from filebrownie.interpretation.extract import interpret_page
from filebrownie.interpretation.vision import parse_page


def page_of(**overrides):
    base = {"document_class": "lab_report", "lab_rows": [], "events": [], "dates": []}
    return parse_page({**base, **overrides})


def lab(label, value, **rest):
    return {"label": label, "value": value, **rest}


def test_n1_minus_sign_cannot_verify_without_sign():
    spans = row(80, [(10, "Base excess"), (150, "-5"), (230, "mmol/L")])
    page = page_of(lab_rows=[lab("Base excess", "5", unit="mmol/L")])
    (fact,) = interpret_page(spans, page).lab_facts
    assert fact.verification == "unverified reading"
    assert "VALUE_NOT_AT_RESULT" in fact.notes


def test_n2_reference_qualitative_cannot_verify_as_result():
    spans = row(80, [(10, "HBsAg"), (150, "Positive"), (320, "Reference: Negative")])
    page = page_of(lab_rows=[lab("HBsAg", "Negative")])
    (fact,) = interpret_page(spans, page).lab_facts
    assert fact.verification == "unverified reading"
    assert "VALUE_NOT_AT_RESULT" in fact.notes


def test_n3_birth_date_cannot_drive_specimen_timeline():
    spans = (
        row(20, [(10, "Specimen: serum")])
        + row(40, [(10, "Date of birth: 01.01.1970")])
        + row(80, [(10, "Ferritin"), (150, "12"), (230, "ng/mL")])
    )
    page = page_of(
        lab_rows=[lab("Ferritin", "12", unit="ng/mL")],
        dates=[{"raw": "01.01.1970", "role": "specimen"}],
    )
    (fact,) = interpret_page(spans, page).lab_facts
    assert fact.timeline.role is None
    assert "DATE_ROLE_UNSUPPORTED" in fact.notes
