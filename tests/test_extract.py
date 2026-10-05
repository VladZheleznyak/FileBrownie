from datetime import date

from support import row

from filebrownie.interpretation.extract import (
    build_timeline,
    interpret_page,
    parse_value,
    wording_types,
)
from filebrownie.interpretation.grounding import PageText, looks_like_lab_row
from filebrownie.interpretation.models import ReportedDate
from filebrownie.interpretation.vision import parse_page


def page_of(**overrides):
    base = {"document_class": "lab_report", "lab_rows": [], "events": [], "dates": []}
    return parse_page({**base, **overrides})


def lab(label, value, **rest):
    return {"label": label, "value": value, **rest}


def table():
    spans = []
    spans += row(20, [(10, "Specimen collected: 12.03.2024")])
    spans += row(40, [(10, "Analyte"), (150, "Result"), (230, "Unit")])
    spans += row(60, [(10, "Hemoglobin"), (150, "13.5"), (230, "g/dL")])
    spans += row(80, [(10, "Ferritin"), (150, "12"), (230, "ng/mL")])
    return spans


def test_row_association_is_verified_with_evidence_and_dates():
    page = page_of(
        lab_rows=[lab("Hemoglobin", "13,5", unit="g/dL")],
        dates=[{"raw": "12.03.2024", "role": "specimen"}],
    )
    (fact,) = interpret_page(table(), page).lab_facts
    assert fact.verification == "verified"
    assert fact.value_number == "13.5" and fact.comparator is None
    assert fact.timeline.role == "specimen"
    assert [item.start for item in fact.timeline.alternatives] == [date(2024, 3, 12)]
    assert fact.evidence  # located spans


def test_matching_number_in_another_row_is_not_enough_and_conflicts():
    spans = table() + row(100, [(10, "Albumin"), (150, "99"), (230, "g/L")])
    page = page_of(lab_rows=[lab("Ferritin", "13.5", unit="ng/mL")])
    (fact,) = interpret_page(spans, page).lab_facts
    assert fact.verification == "conflicting"
    assert fact.alternative_label == "Hemoglobin"
    assert fact.evidence and fact.alternative_evidence


def test_value_absent_from_text_is_stored_unverified():
    page = page_of(lab_rows=[lab("Hemoglobin", "99.9", unit="g/dL")])
    (fact,) = interpret_page(table(), page).lab_facts
    assert fact.verification == "unverified reading"
    assert fact.raw_value == "99.9"


def test_lab_timeline_uses_supported_report_when_specimen_role_is_unsupported():
    spans = row(20, [(10, "Report issued: 05/09/2023")]) + row(
        80, [(10, "Ferritin"), (150, "186"), (230, "ug/L")]
    )
    page = page_of(
        lab_rows=[
            lab(
                "Ferritin",
                "186",
                unit="ug/L",
                dates=[{"raw": "05/04/2023", "role": "specimen"}],
            )
        ],
        dates=[{"raw": "05/09/2023", "role": "report"}],
    )
    (fact,) = interpret_page(spans, page).lab_facts
    assert fact.timeline.role == "report"
    assert fact.timeline.alternatives[0].start == date(2023, 9, 5)


def test_unlocated_lab_row_without_evidence_is_stored_unverified():
    spans = row(40, [(10, "Ferritin")]) + row(200, [(10, "99"), (100, "ng/mL")])
    page = page_of(lab_rows=[lab("Ferritin", "99", unit="ng/mL")])
    (fact,) = interpret_page(spans, page).lab_facts
    assert fact.verification == "unverified reading"
    assert {"NOT_LOCATED", "ASSOCIATION_NOT_LOCATED"} & set(fact.notes)


def test_integer_value_with_trailing_period_verifies_when_row_matches():
    spans = row(80, [(10, "Ferritin"), (150, "186"), (230, "ug/L"), (320, "22-537")])
    page = page_of(lab_rows=[lab("Ferritin", "186.", unit="ug/L")])
    (fact,) = interpret_page(spans, page).lab_facts
    assert fact.verification == "verified"
    assert fact.value_number == "186"


def test_reference_interval_value_does_not_verify_as_result():
    spans = row(80, [(10, "Ferritin"), (150, "12"), (230, "ng/mL"), (320, "15-150")])
    page = page_of(lab_rows=[lab("Ferritin", "15", unit="ng/mL")])
    (fact,) = interpret_page(spans, page).lab_facts
    assert fact.verification == "unverified reading"
    assert "VALUE_NOT_AT_RESULT" in fact.notes


def test_reference_threshold_does_not_verify_as_result():
    spans = row(80, [(10, "Ferritin"), (150, "12"), (230, "ng/mL"), (320, ">15")])
    page = page_of(lab_rows=[lab("Ferritin", "15", unit="ng/mL")])
    (fact,) = interpret_page(spans, page).lab_facts
    assert fact.verification == "unverified reading"
    assert "VALUE_NOT_AT_RESULT" in fact.notes


def test_comparator_must_match_located_result():
    spans = row(80, [(10, "Ferritin"), (150, "<5"), (230, "ng/mL")])
    page = page_of(lab_rows=[lab("Ferritin", "5", unit="ng/mL")])
    (fact,) = interpret_page(spans, page).lab_facts
    assert fact.verification == "unverified reading"
    assert "VALUE_NOT_AT_RESULT" in fact.notes


def test_comparator_is_preserved_when_located():
    spans = row(80, [(10, "Ferritin"), (150, "<5"), (230, "ng/mL")])
    page = page_of(lab_rows=[lab("Ferritin", "<5", unit="ng/mL")])
    (fact,) = interpret_page(spans, page).lab_facts
    assert fact.verification == "verified"
    assert fact.comparator == "<" and fact.value_number == "5"


def test_negative_sign_must_match_located_result():
    spans = row(80, [(10, "Base excess"), (150, "-5"), (230, "mmol/L")])
    page = page_of(lab_rows=[lab("Base excess", "5", unit="mmol/L")])
    (fact,) = interpret_page(spans, page).lab_facts
    assert fact.verification == "unverified reading"
    assert "VALUE_NOT_AT_RESULT" in fact.notes


def test_negative_sign_is_preserved_when_located():
    spans = row(80, [(10, "Base excess"), (150, "-5"), (230, "mmol/L")])
    page = page_of(lab_rows=[lab("Base excess", "-5", unit="mmol/L")])
    (fact,) = interpret_page(spans, page).lab_facts
    assert fact.verification == "verified"
    assert fact.raw_value == "-5"
    assert fact.value_number == "-5" and fact.comparator is None


def test_two_character_comparator_must_match_located_result():
    spans = row(80, [(10, "Ferritin"), (150, "<=5"), (230, "ng/mL")])
    unsigned = page_of(lab_rows=[lab("Ferritin", "5", unit="ng/mL")])
    (fact,) = interpret_page(spans, unsigned).lab_facts
    assert fact.verification == "unverified reading"
    assert "VALUE_NOT_AT_RESULT" in fact.notes
    spans = row(80, [(10, "Ferritin"), (150, ">=5"), (230, "ng/mL")])
    (fact,) = interpret_page(spans, unsigned).lab_facts
    assert fact.verification == "unverified reading"
    assert "VALUE_NOT_AT_RESULT" in fact.notes


def test_two_character_comparator_is_preserved_when_located():
    spans = row(80, [(10, "Ferritin"), (150, "<=5"), (230, "ng/mL")])
    page = page_of(lab_rows=[lab("Ferritin", "<=5", unit="ng/mL")])
    (fact,) = interpret_page(spans, page).lab_facts
    assert fact.verification == "verified"
    assert fact.comparator == "<=" and fact.value_number == "5"
    spans = row(80, [(10, "Ferritin"), (150, ">=5"), (230, "ng/mL")])
    page = page_of(lab_rows=[lab("Ferritin", ">=5", unit="ng/mL")])
    (fact,) = interpret_page(spans, page).lab_facts
    assert fact.verification == "verified"
    assert fact.comparator == ">=" and fact.value_number == "5"


def test_plus_and_unicode_minus_must_match_located_result():
    spans = row(80, [(10, "Base excess"), (150, "+2"), (230, "mmol/L")])
    unsigned = page_of(lab_rows=[lab("Base excess", "2", unit="mmol/L")])
    (fact,) = interpret_page(spans, unsigned).lab_facts
    assert fact.verification == "unverified reading"
    assert "VALUE_NOT_AT_RESULT" in fact.notes
    signed = page_of(lab_rows=[lab("Base excess", "+2", unit="mmol/L")])
    (fact,) = interpret_page(spans, signed).lab_facts
    assert fact.verification == "verified"
    assert fact.value_number == "+2"
    spans = row(80, [(10, "Base excess"), (150, "\u22125"), (230, "mmol/L")])
    unsigned = page_of(lab_rows=[lab("Base excess", "5", unit="mmol/L")])
    (fact,) = interpret_page(spans, unsigned).lab_facts
    assert fact.verification == "unverified reading"
    assert "VALUE_NOT_AT_RESULT" in fact.notes
    ascii_sign = page_of(lab_rows=[lab("Base excess", "-5", unit="mmol/L")])
    (fact,) = interpret_page(spans, ascii_sign).lab_facts
    assert fact.verification == "verified" and fact.value_number == "-5"
    unicode_sign = page_of(lab_rows=[lab("Base excess", "\u22125", unit="mmol/L")])
    (fact,) = interpret_page(spans, unicode_sign).lab_facts
    assert fact.verification == "verified" and fact.value_number == "-5"


def test_reference_qualitative_value_does_not_verify_as_result():
    spans = row(80, [(10, "HBsAg"), (150, "Positive"), (320, "Reference: Negative")])
    page = page_of(lab_rows=[lab("HBsAg", "Negative")])
    (fact,) = interpret_page(spans, page).lab_facts
    assert fact.verification == "unverified reading"
    assert "VALUE_NOT_AT_RESULT" in fact.notes


def test_qualitative_result_at_result_position_verifies():
    spans = row(80, [(10, "HBsAg"), (150, "Positive"), (320, "Reference: Negative")])
    page = page_of(lab_rows=[lab("HBsAg", "Positive")])
    (fact,) = interpret_page(spans, page).lab_facts
    assert fact.verification == "verified"


def test_unit_from_another_analyte_row_is_not_inherited():
    spans = row(60, [(10, "Ferritin"), (150, "12"), (230, "ng/mL")]) + row(
        80, [(10, "Hemoglobin"), (150, "135"), (230, "g/L")]
    )
    page = page_of(lab_rows=[lab("Hemoglobin", "135", unit="ng/mL")])
    (fact,) = interpret_page(spans, page).lab_facts
    assert fact.verification == "unverified reading"
    assert "UNIT_NOT_LOCATED" in fact.notes


def test_fabricated_reference_interval_and_flag_are_omitted():
    spans = row(80, [(10, "Ferritin"), (150, "12"), (230, "ng/mL")])
    page = page_of(
        lab_rows=[lab("Ferritin", "12", unit="ng/mL", reference_interval="99-999", flag="H")]
    )
    (fact,) = interpret_page(spans, page).lab_facts
    assert fact.verification == "unverified reading"
    assert fact.reference_interval is None and fact.flag is None
    assert "REFERENCE_NOT_LOCATED" in fact.notes and "FLAG_NOT_LOCATED" in fact.notes


def test_report_date_cannot_become_specimen_timeline():
    spans = row(20, [(10, "Report issued: 12.03.2024")]) + row(
        80, [(10, "Ferritin"), (150, "12"), (230, "ng/mL")]
    )
    page = page_of(
        lab_rows=[lab("Ferritin", "12", unit="ng/mL")],
        dates=[{"raw": "12.03.2024", "role": "specimen"}],
    )
    (fact,) = interpret_page(spans, page).lab_facts
    assert fact.timeline.role is None
    assert "DATE_ROLE_UNSUPPORTED" in fact.notes
    (unsupported,) = [d for d in fact.dates if d.raw == "12.03.2024"]
    assert unsupported.role == "specimen" and not unsupported.role_supported


def test_header_unit_does_not_replace_the_unit_on_the_row():
    spans = row(20, [(10, "Units: mmol/L")]) + row(
        80, [(10, "Hemoglobin"), (150, "13.5"), (230, "g/dL")]
    )
    claimed = page_of(lab_rows=[lab("Hemoglobin", "13.5", unit="mmol/L")])
    (fact,) = interpret_page(spans, claimed).lab_facts
    assert fact.verification == "unverified reading"
    assert "UNIT_NOT_LOCATED" in fact.notes
    located = page_of(lab_rows=[lab("Hemoglobin", "13.5", unit="g/dL")])
    (fact,) = interpret_page(spans, located).lab_facts
    assert fact.verification == "verified" and fact.unit == "g/dL"


def test_birth_date_cannot_inherit_specimen_timeline_role():
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
    (unsupported,) = [d for d in fact.dates if d.raw == "01.01.1970"]
    assert unsupported.role == "specimen" and not unsupported.role_supported


def test_printed_date_does_not_inherit_specimen_role():
    ferritin = row(80, [(10, "Ferritin"), (150, "12"), (230, "ng/mL")])
    cases = (
        (
            row(20, [(10, "Specimen collected: 12.03.2024")])
            + row(40, [(10, "Printed: 01.01.2020")])
            + ferritin,
            "01.01.2020",
        ),
        (
            row(20, [(10, "Specimen collected: 12.03.2024")])
            + row(40, [(10, "01.01.1970")])
            + ferritin,
            "01.01.1970",
        ),
        (
            row(20, [(10, "Specimen: serum")]) + row(40, [(10, "Printed: 01.01.2020")]) + ferritin,
            "01.01.2020",
        ),
    )
    for spans, raw in cases:
        page = page_of(
            lab_rows=[lab("Ferritin", "12", unit="ng/mL")],
            dates=[{"raw": raw, "role": "specimen"}],
        )
        (fact,) = interpret_page(spans, page).lab_facts
        assert fact.timeline.role is None
        assert "DATE_ROLE_UNSUPPORTED" in fact.notes


def test_lab_date_role_uses_caption_row_above_split_date_span():
    spans = (
        row(20, [(10, "Дата забора")])
        + row(40, [(10, "15.06.2023")])
        + row(80, [(10, "Ferritin"), (150, "12"), (230, "ng/mL")])
    )
    page = page_of(
        lab_rows=[lab("Ferritin", "12", unit="ng/mL")],
        dates=[{"raw": "15.06.2023", "role": "specimen"}],
    )
    (fact,) = interpret_page(spans, page).lab_facts
    assert fact.timeline.role == "specimen"


def test_integer_result_with_trailing_period_verifies():
    spans = row(80, [(10, "FERRITIN"), (150, "186."), (230, "22 - 537"), (320, "ug/L")])
    page = page_of(
        lab_rows=[lab("FERRITIN", "186", unit="ug/L", reference_interval="22 - 537")],
    )
    (fact,) = interpret_page(spans, page).lab_facts
    assert fact.verification == "verified"
    assert fact.value_number == "186"


def test_two_column_header_assigns_specimen_and_report_dates():
    spans = (
        row(20, [(10, "Collected"), (220, "Reported")])
        + row(40, [(10, "2025 Jul 04, 07:00"), (220, "2025 Aug 20, 21:02")])
        + row(80, [(10, "FERRITIN"), (150, "147"), (230, "ug/L")])
    )
    page = page_of(
        lab_rows=[lab("FERRITIN", "147", unit="ug/L")],
        dates=[
            {"raw": "2025 Jul 04, 07:00", "role": "specimen"},
            {"raw": "2025 Aug 20, 21:02", "role": "report"},
        ],
    )
    (fact,) = interpret_page(spans, page).lab_facts
    assert fact.timeline.role == "specimen"
    roles = {item.raw: item.role_supported for item in fact.dates}
    assert roles["2025 Jul 04, 07:00"] and roles["2025 Aug 20, 21:02"]


def test_split_date_wording_locates_for_timeline():
    spans = (
        row(20, [(10, "Specimen collected:")])
        + row(40, [(10, "2023 May 04,"), (140, "17:45")])
        + row(80, [(10, "FERRITIN"), (150, "186"), (230, "ug/L")])
    )
    page = page_of(
        lab_rows=[lab("FERRITIN", "186", unit="ug/L")],
        dates=[{"raw": "2023 May 04, 17:45", "role": "specimen"}],
    )
    (fact,) = interpret_page(spans, page).lab_facts
    assert "DATE_NOT_LOCATED" not in fact.notes
    assert fact.timeline.role == "specimen"


def test_ocr_damaged_unit_alias_still_verifies():
    spans = row(80, [(10, "FERRITIN"), (150, "171"), (230, "ugn.")])
    page = page_of(lab_rows=[lab("Ferritin", "171", unit="ug/L")])
    (fact,) = interpret_page(spans, page).lab_facts
    assert fact.verification == "verified"


def test_lab_date_role_uses_nearest_cue_to_the_left():
    spans = row(
        20,
        [(10, "Specimen collected: 01.03.2024"), (200, "Report issued: 02.03.2024")],
    ) + row(80, [(10, "Ferritin"), (150, "12"), (230, "ng/mL")])
    page = page_of(
        lab_rows=[lab("Ferritin", "12", unit="ng/mL")],
        dates=[
            {"raw": "01.03.2024", "role": "specimen"},
            {"raw": "02.03.2024", "role": "report"},
        ],
    )
    (fact,) = interpret_page(spans, page).lab_facts
    assert fact.timeline.role == "specimen"
    roles = {item.raw: item for item in fact.dates}
    assert roles["01.03.2024"].role_supported and roles["02.03.2024"].role_supported


def test_letterhead_wording_does_not_create_an_event_row():
    spans = row(20, [(10, "Urology clinic letterhead")])
    page = page_of(
        document_class="visit_note",
        events=[
            {
                "specialty": "urology",
                "event_type": "encounter",
                "wording": "Urology clinic letterhead",
            }
        ],
    )
    assert interpret_page(spans, page).events == ()


def test_negated_consultation_is_not_a_verified_encounter():
    spans = row(20, [(10, "No urology consultation occurred")])
    page = page_of(
        document_class="visit_note",
        events=[
            {
                "specialty": "urology",
                "event_type": "encounter",
                "wording": "No urology consultation occurred",
            }
        ],
    )
    assert interpret_page(spans, page).events == ()


def test_cancellation_in_the_following_sentence_is_not_an_encounter():
    wording = "Urology consultation. This visit was cancelled."
    spans = row(20, [(10, wording)])
    page = page_of(
        document_class="visit_note",
        events=[{"specialty": "urology", "event_type": "encounter", "wording": wording}],
    )
    assert interpret_page(spans, page).events == ()
    assert wording_types(wording) == set()
    kept = "Urology consultation occurred. No fever was reported."
    spans = row(20, [(10, kept)])
    page = page_of(
        document_class="visit_note",
        events=[{"specialty": "urology", "event_type": "encounter", "wording": kept}],
    )
    (fact,) = interpret_page(spans, page).events
    assert fact.verification == "verified" and fact.event_type == "encounter"


def test_cancelled_consultation_is_not_a_verified_encounter():
    spans = row(20, [(10, "Consultation with urology was cancelled")])
    page = page_of(
        document_class="visit_note",
        events=[
            {
                "specialty": "urology",
                "event_type": "encounter",
                "wording": "Consultation with urology was cancelled",
            }
        ],
    )
    assert interpret_page(spans, page).events == ()


def test_long_negated_consultation_is_not_a_verified_encounter():
    spans = row(20, [(10, "No prior or subsequent urology consultation occurred")])
    page = page_of(
        document_class="visit_note",
        events=[
            {
                "specialty": "urology",
                "event_type": "encounter",
                "wording": "No prior or subsequent urology consultation occurred",
            }
        ],
    )
    assert interpret_page(spans, page).events == ()


def test_model_wording_fragment_cannot_bypass_source_negation():
    spans = row(20, [(10, "No urology consultation occurred")])
    page = page_of(
        document_class="visit_note",
        events=[
            {
                "specialty": "urology",
                "event_type": "encounter",
                "wording": "urology consultation occurred",
            }
        ],
    )
    assert interpret_page(spans, page).events == ()


def test_unit_not_in_row_or_header_keeps_reading_unverified():
    page = page_of(lab_rows=[lab("Hemoglobin", "13.5", unit="mmol/L")])
    (fact,) = interpret_page(table(), page).lab_facts
    assert fact.verification == "unverified reading"
    assert "UNIT_NOT_LOCATED" in fact.notes


def test_unit_may_be_grounded_in_header_row_above():
    spans = row(20, [(10, "Result in g/dL")]) + row(40, [(10, "Hemoglobin"), (150, "13.5")])
    page = page_of(lab_rows=[lab("Hemoglobin", "13.5", unit="g/dL")])
    (fact,) = interpret_page(spans, page).lab_facts
    assert fact.verification == "verified"


def test_missing_fields_stay_missing():
    page = page_of(lab_rows=[lab("Hemoglobin", "13.5")])
    (fact,) = interpret_page(table(), page).lab_facts
    assert fact.unit is None and fact.reference_interval is None and fact.dates == ()
    assert fact.timeline.role is None and fact.verification == "verified"


def test_ungrounded_date_prevents_verification_but_keeps_reading():
    page = page_of(
        lab_rows=[lab("Hemoglobin", "13.5", unit="g/dL")],
        dates=[{"raw": "01.01.2020", "role": "specimen"}],
    )
    (fact,) = interpret_page(table(), page).lab_facts
    assert fact.verification == "unverified reading" and "DATE_NOT_LOCATED" in fact.notes


def test_missing_context_page_never_yields_verified_rows():
    page = page_of(lab_rows=[lab("Hemoglobin", "13.5")], context_missing=True)
    result = interpret_page(table(), page)
    assert result.lab_facts[0].verification == "unverified reading"
    assert "MISSING_CONTEXT" in {item.code for item in result.warnings}


def test_spans_without_boxes_ground_only_inside_a_single_span():
    from filebrownie.evidence.models import TextSpan

    together = [TextSpan("Hemoglobin 13.5 g/dL", None)]
    apart = [TextSpan("Hemoglobin", None), TextSpan("13.5", None)]
    page = page_of(lab_rows=[lab("Hemoglobin", "13.5", unit="g/dL")])
    assert interpret_page(together, page).lab_facts[0].verification == "verified"
    (apart_fact,) = interpret_page(apart, page).lab_facts
    assert apart_fact.verification == "unverified reading"
    assert "ASSOCIATION_NOT_LOCATED" in apart_fact.notes


def test_same_value_in_two_rows_verifies_against_the_correct_row():
    spans = row(60, [(10, "Hemoglobin"), (150, "5.1")]) + row(80, [(10, "Ferritin"), (150, "5.1")])
    page = page_of(lab_rows=[lab("Ferritin", "5.1"), lab("Hemoglobin", "5.1")])
    facts = interpret_page(spans, page).lab_facts
    assert [fact.verification for fact in facts] == ["verified", "verified"]
    assert facts[0].evidence != facts[1].evidence


def test_parse_value_comparators_qualitative_and_decimals():
    assert parse_value("<5") == ("<", "5", False)
    assert parse_value("<=5") == ("<=", "5", False)
    assert parse_value(">= 5") == (">=", "5", False)
    assert parse_value("> 100,5") == (">", "100.5", False)
    assert parse_value("-5") == (None, "-5", False)
    assert parse_value("+2") == (None, "+2", False)
    assert parse_value("\u22125") == (None, "-5", False)
    assert parse_value("negative") == (None, None, True)
    assert parse_value("1:80") == (None, None, False)


def test_timeline_prefers_specimen_then_report_and_preserves_ambiguity():
    from filebrownie.interpretation.dates import parse_date

    def reported(raw, role):
        return ReportedDate(raw, role, parse_date(raw), (0,))

    dates = [reported("01.04.2024", "report"), reported("03/04/2024", "specimen")]
    timeline = build_timeline(dates, ("specimen", "report", "unspecified"))
    assert timeline.role == "specimen" and len(timeline.alternatives) == 2
    only_report = build_timeline(dates[:1], ("specimen", "report"))
    assert only_report.role == "report"
    assert build_timeline([], ("specimen",)).role is None


def test_prose_with_numeric_range_is_not_a_lab_row():
    prose = row(80, [(10, "Some men (about 1 in 20-50) will develop swelling")])
    assert not looks_like_lab_row(PageText.build(prose).rows[0])


def test_dense_canadian_panel_line_is_a_lab_row():
    panel = row(
        80,
        [
            (
                10,
                "12 FERRITIN TEST STATUS Final REFERENCE 22 - 537 YOUR RESULT 186 ug/L normal",
            )
        ],
    )
    assert looks_like_lab_row(PageText.build(panel).rows[0])


def test_unparseable_located_date_is_not_stored():
    spans = row(20, [(10, "Specimen collected: 6")]) + row(
        80, [(10, "Ferritin"), (150, "12"), (230, "ng/mL")]
    )
    page = page_of(
        lab_rows=[lab("Ferritin", "12", unit="ng/mL")],
        dates=[{"raw": "6", "role": "specimen"}],
    )
    (fact,) = interpret_page(spans, page).lab_facts
    assert not [item for item in fact.dates if item.raw == "6"]


def test_undetected_lab_rows_raise_incomplete_extraction_warning():
    spans = table() + row(100, [(10, "Albumin"), (150, "41"), (230, "g/L")])
    page = page_of(lab_rows=[lab("Hemoglobin", "13.5", unit="g/dL")])
    warnings = interpret_page(spans, page).warnings
    flagged = [item for item in warnings if item.code == "POSSIBLE_INCOMPLETE_TABLE_EXTRACTION"]
    assert len(flagged) == 2  # Ferritin and Albumin rows have no facts
    assert looks_like_lab_row(PageText.build(table()).rows[2])
    assert not looks_like_lab_row(PageText.build(table()).rows[1])


def event_spans():
    return (
        row(20, [(10, "Referral to urologist")])
        + row(40, [(10, "Date: 05.04.2024")])
        + row(300, [(10, "Clinic urology department letterhead")])
    )


def event(**rest):
    base = {
        "specialty": "urologist",
        "event_type": "referral",
        "wording": "Referral to urologist",
        "dates": [{"raw": "05.04.2024", "role": "event"}],
    }
    return {**base, **rest}


def test_referral_event_is_grounded_and_direct_not_an_encounter():
    page = page_of(document_class="referral", events=[event()])
    (fact,) = interpret_page(event_spans(), page).events
    assert fact.verification == "verified" and fact.strength == "direct"
    assert fact.event_type == "referral" and fact.timeline.role == "event"


def test_specialty_word_away_from_wording_is_not_verified():
    spans = row(20, [(10, "Referral for tests")]) + row(300, [(10, "urologist")])
    page = page_of(
        document_class="referral",
        events=[event(wording="Referral for tests", dates=[])],
    )
    (fact,) = interpret_page(spans, page).events
    assert fact.verification == "unverified reading"


def test_event_type_disagreeing_with_wording_is_conflicting_candidate():
    page = page_of(
        document_class="visit_note",
        events=[event(event_type="encounter", dates=[])],
    )
    (fact,) = interpret_page(event_spans(), page).events
    assert fact.verification == "conflicting" and fact.alternative_event_type == "referral"


def test_recommendation_stays_other_and_cannot_become_a_referral_or_visit():
    spans = row(20, [(10, "Recommended consultation with urologist")])
    wording = "Recommended consultation with urologist"
    ok = page_of(
        document_class="visit_note",
        events=[event(event_type="other", recommendation=True, wording=wording, dates=[])],
    )
    assert interpret_page(spans, ok).events[0].verification == "verified"
    bad = page_of(
        document_class="visit_note",
        events=[event(event_type="encounter", wording=wording, dates=[])],
    )
    assert interpret_page(spans, bad).events[0].verification == "conflicting"
    assert wording_types("Рекомендована консультація уролога") == {"other"}


def test_events_without_wording_are_mention_only_and_handwriting_is_weak():
    page = page_of(events=[event(wording="")], handwriting=True)
    result = interpret_page(event_spans(), page)
    assert result.events == ()
    assert "HANDWRITING_NOT_INTERPRETED" in {item.code for item in result.warnings}
    weak = page_of(document_class="referral", events=[event()], handwriting=True)
    assert interpret_page(event_spans(), weak).events[0].strength == "weak"


def test_indirect_strength_when_another_document_reports_the_event():
    page = page_of(document_class="other", events=[event(dates=[])])
    assert interpret_page(event_spans(), page).events[0].strength == "indirect"


def test_event_report_date_never_becomes_event_date():
    page = page_of(
        document_class="referral",
        events=[event(dates=[{"raw": "05.04.2024", "role": "report"}])],
    )
    (fact,) = interpret_page(event_spans(), page).events
    assert fact.timeline.role is None and fact.timeline.alternatives == ()


def test_placeholder_items_are_ignored_and_partial_items_are_counted():
    from filebrownie.interpretation.vision import parse_page

    page = parse_page(
        {
            "lab_rows": [{"label": "Ferritin", "value": "12"}, {"label": "", "value": ""}],
            "events": [{"specialty": "", "event_type": "result", "wording": ""}],
        }
    )
    assert len(page.lab_rows) == 1 and page.dropped == 0
    page = parse_page(
        {"lab_rows": [{"label": "Ferritin", "value": ""}], "events": [{"specialty": ""}]}
    )
    assert not page.lab_rows and page.dropped == 1
    page = parse_page({"lab_rows": [{"label": "ferritin", "value": "X"}]})
    assert not page.lab_rows and page.dropped == 1
    page = parse_page({"lab_rows": [{"label": "Ferritin", "value": "pending"}]})
    assert not page.lab_rows and page.dropped == 1


def test_specimen_the_page_never_states_is_discarded_without_demoting_the_reading():
    spans = [*row(80, [(20, "Ferritin"), (200, "12"), (280, "ng/mL")])]
    page = parse_page(
        {"lab_rows": [{"label": "Ferritin", "value": "12", "unit": "ng/mL", "specimen": "Blood"}]}
    )
    (fact,) = interpret_page(tuple(spans), page).lab_facts
    assert fact.verification == "verified" and fact.specimen is None
    assert "SPECIMEN_NOT_LOCATED" in fact.notes
