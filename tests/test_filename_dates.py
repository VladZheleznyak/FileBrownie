from datetime import date

from support import row

from filebrownie.interpretation.extract import interpret_page
from filebrownie.interpretation.filename_dates import consensus_filename_dates, dates_from_path
from filebrownie.interpretation.vision import parse_page


def test_dates_from_path_basename_patterns():
    assert dates_from_path("2025-12-08bw.jpg")[0].start == date(2025, 12, 8)
    assert dates_from_path("folder/2025-07-04 bw.pdf")[0].start == date(2025, 7, 4)
    dotted = dates_from_path("нога/2011.07.23 15.31.53 004.jpg")
    assert dotted[0].start == date(2011, 7, 23)


def test_consensus_requires_agreement():
    one = ("2025-12-08bw.jpg",)
    assert consensus_filename_dates(one)
    conflict = ("2025-12-08bw.jpg", "2025-07-04 bw.pdf")
    assert not consensus_filename_dates(conflict)


def test_filename_timeline_when_page_has_no_grounded_dates():
    spans = row(80, [(10, "Ferritin"), (150, "171"), (230, "ug/L")])
    page = parse_page({"lab_rows": [{"label": "FERRITIN", "value": "171", "unit": "ug/L"}]})
    (fact,) = interpret_page(spans, page, ("2025-12-08bw.jpg",)).lab_facts
    assert fact.timeline.role == "filename"
    assert fact.timeline.alternatives[0].start == date(2025, 12, 8)
    assert "FILENAME_DATE_INFERRED" in fact.notes


def test_filename_timeline_ignored_when_specimen_date_is_grounded():
    spans = row(20, [(10, "Collected 12.03.2024")]) + row(
        80, [(10, "Ferritin"), (150, "12"), (230, "ng/mL")]
    )
    page = parse_page(
        {
            "lab_rows": [{"label": "Ferritin", "value": "12", "unit": "ng/mL"}],
            "dates": [{"raw": "12.03.2024", "role": "specimen"}],
        }
    )
    (fact,) = interpret_page(spans, page, ("2025-12-08bw.jpg",)).lab_facts
    assert fact.timeline.role == "specimen"
    assert "FILENAME_DATE_INFERRED" not in fact.notes


def test_ambiguous_slash_date_aligns_with_filename_path():
    spans = row(20, [(10, "Specimen collected: 05/04/2023")]) + row(
        80, [(10, "Ferritin"), (150, "186"), (230, "ug/L")]
    )
    page = parse_page(
        {
            "lab_rows": [
                {
                    "label": "FERRITIN",
                    "value": "186",
                    "unit": "ug/L",
                    "dates": [{"raw": "05/04/2023", "role": "specimen"}],
                }
            ],
            "dates": [{"raw": "05/09/2023", "role": "report"}],
        }
    )
    path = ("reports/2023-05-04 sample-bw.pdf",)
    (fact,) = interpret_page(spans, page, path).lab_facts
    (specimen,) = [item for item in fact.dates if item.raw == "05/04/2023"]
    assert specimen.alternatives[0].start == date(2023, 5, 4)
    assert len(specimen.alternatives) == 1
    assert "FILENAME_DATE_ALIGNED" in fact.notes
