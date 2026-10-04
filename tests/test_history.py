from contextlib import contextmanager
from datetime import date

import pytest
from support import FakeVision, pdf_lines

from filebrownie.ingestion.scan import run_full_scan
from filebrownie.interpretation.dates import parse_date
from filebrownie.presentation import cli
from filebrownie.query.history import QueryError, coverage_warnings, run_query
from filebrownie.query.timeline import (
    IN_RANGE,
    MAY_FALL,
    OUT,
    UNCERTAIN,
    UNDATED,
    RangeError,
    classify,
    parse_bound,
    parse_range,
)


def test_range_bounds_expand_to_calendar_periods():
    assert parse_bound("2024", False) == date(2024, 1, 1)
    assert parse_bound("2024", True) == date(2024, 12, 31)
    assert parse_bound("2024-02", True) == date(2024, 2, 29)
    assert parse_bound("2024-02-10", False) == date(2024, 2, 10)
    for bad in ("2024-13", "yesterday", "2024-02-30"):
        with pytest.raises(RangeError):
            parse_bound(bad, False)
    with pytest.raises(RangeError):
        parse_range("2025", "2024")


def test_classification_keeps_precision_and_ambiguity():
    window = parse_range("2024-04-01", "2024-04-30")
    assert classify((), window) == UNDATED
    assert classify(parse_date("12.04.2024"), window) == IN_RANGE
    assert classify(parse_date("12.05.2024"), window) == OUT
    assert classify(parse_date("04.2024"), window) == IN_RANGE
    assert classify(parse_date("2024"), window) == MAY_FALL
    assert classify(parse_date("04.2024"), parse_range("2024-04-15", "2024-04-30")) == MAY_FALL
    # 03/04/2024 may be 3 April or 4 March: one overlaps, so it is date-uncertain.
    assert classify(parse_date("03/04/2024"), window) == UNCERTAIN
    assert classify(parse_date("03/04/2024"), parse_range("2023", "2023")) == OUT
    assert classify(parse_date("03/04/2024"), parse_range(None, None)) == UNCERTAIN


def doc(source, name, lines, rows, dates=(), **rest):
    pdf_lines(source / name, lines)
    return {"document_class": "lab_report", "lab_rows": rows, "dates": list(dates), **rest}


@pytest.fixture
def folders(tmp_path, repository):
    source, data = tmp_path / "sources", tmp_path / "data"
    source.mkdir()
    data.mkdir()
    repository.migrate()
    return source, data


@pytest.fixture
def collection(repository, folders):
    source, data = folders
    outputs = [
        doc(  # a: specimen day date
            source,
            "a.pdf",
            [
                (20, 40, "Specimen 12.03.2024"),
                (20, 80, "Hemoglobin"),
                (200, 80, "13.5"),
                (280, 80, "g/dL"),
            ],
            [{"label": "Hemoglobin", "value": "13.5", "unit": "g/dL"}],
            [{"raw": "12.03.2024", "role": "specimen"}],
        ),
        doc(  # b: Russian label and unit, 2023
            source,
            "b.pdf",
            [
                (20, 40, "Дата забора 15.06.2023"),
                (20, 80, "Гемоглобин"),
                (200, 80, "128"),
                (280, 80, "г/л"),
            ],
            [{"label": "Гемоглобин", "value": "128", "unit": "г/л"}],
            [{"raw": "15.06.2023", "role": "specimen"}],
        ),
        doc(  # c: ambiguous slash date
            source,
            "c.pdf",
            [
                (20, 40, "Date 03/04/2024"),
                (20, 80, "Hemoglobin"),
                (200, 80, "12.9"),
                (280, 80, "g/dL"),
            ],
            [{"label": "Hemoglobin", "value": "12.9", "unit": "g/dL"}],
            [{"raw": "03/04/2024", "role": "specimen"}],
        ),
        doc(  # d: no date at all
            source,
            "d.pdf",
            [(20, 80, "Hemoglobin"), (200, 80, "11.0"), (280, 80, "g/dL")],
            [{"label": "Hemoglobin", "value": "11.0", "unit": "g/dL"}],
        ),
        doc(  # e: only a mention in prose plus a fact for another analyte
            source,
            "e.pdf",
            [
                (20, 40, "Comment: hemoglobin to be rechecked"),
                (20, 80, "Albumin"),
                (200, 80, "41"),
                (280, 80, "g/L"),
            ],
            [{"label": "Albumin", "value": "41", "unit": "g/L"}],
        ),
        doc(  # f: month precision
            source,
            "f.pdf",
            [
                (20, 40, "Specimen 05.2024"),
                (20, 80, "Hemoglobin"),
                (200, 80, "14.2"),
                (280, 80, "g/dL"),
            ],
            [{"label": "Hemoglobin", "value": "14.2", "unit": "g/dL"}],
            [{"raw": "05.2024", "role": "specimen"}],
        ),
        doc(  # g: number matches another row -> conflicting
            source,
            "g.pdf",
            [
                (20, 40, "Specimen 01.02.2022"),
                (20, 80, "Albumin"),
                (200, 80, "9.9"),
                (280, 80, "g/dL"),
                (20, 100, "Hemoglobin"),
                (200, 100, "15.5"),
                (280, 100, "g/dL"),
            ],
            [{"label": "Albumin", "value": "15.5", "unit": "g/dL"}],
            [{"raw": "01.02.2022", "role": "specimen"}],
        ),
    ]
    vision = FakeVision(outputs)
    outcome = run_full_scan(repository, source, data, None, vision)
    assert outcome.activation.activated
    return repository, source, data


def rows_of(result):
    return sorted(row.fields["value"] for row in result.rows)


def test_hemoglobin_history_across_languages_with_sections(collection):
    repository, *_ = collection
    for term in ("hemoglobin", "ГЕМОГЛОБИН", "гемоглобін", "hgb"):
        result = run_query(repository, "labs", term, parse_range(None, None))
        assert rows_of(result) == ["128", "13.5", "14.2"]
        assert [row.fields["value"] for row in result.undated] == ["11.0"]
        assert {c.row.fields["value"] for c in result.candidates} == {"12.9", "15.5"}
    result = run_query(repository, "labs", "hemoglobin", parse_range(None, None))
    candidates = {c.row.fields["value"]: c.reasons for c in result.candidates}
    assert any("date uncertain" in reason for reason in candidates["12.9"])
    assert any("unresolved reading" in reason for reason in candidates["15.5"])
    ordered = [row.date_text for row in result.rows]
    assert ordered == sorted(ordered, key=lambda text: text.split(" ")[0])
    assert any(
        "units" in note.lower() or "different units" in note.lower() for note in result.notes
    )
    assert result.empty is False


def test_date_filter_excludes_undated_but_counts_them(collection):
    repository, *_ = collection
    result = run_query(repository, "labs", "hemoglobin", parse_range("2024-01-01", "2024-12-31"))
    assert rows_of(result) == ["13.5", "14.2"]
    assert result.undated == [] and result.excluded_undated == 1
    # 05.2024 lies fully inside the window; narrowing to mid-May makes it a "may fall" row.
    narrow = run_query(repository, "labs", "hemoglobin", parse_range("2024-05-15", "2024-05-31"))
    assert [row.fields["value"] for row in narrow.rows] == ["14.2"]
    assert "may fall within range" in narrow.rows[0].markers
    april = run_query(repository, "labs", "hemoglobin", parse_range("2024-04-01", "2024-04-30"))
    assert [c.row.fields["value"] for c in april.candidates][:1] == ["12.9"]
    assert "date uncertain" in april.candidates[0].reasons[0]


def test_unmatched_mention_is_reported_even_next_to_facts(collection):
    repository, *_ = collection
    result = run_query(repository, "labs", "hemoglobin", parse_range(None, None))
    texts = [mention.text for mention in result.mentions]
    assert any("rechecked" in text for text in texts)
    # Spans that back matching facts are not reported as unmatched mentions.
    assert not any(
        text == "Hemoglobin" and "a.pdf" in m.sources for m in result.mentions for text in [m.text]
    )


def test_unknown_term_searches_raw_labels_and_empty_result_is_careful(collection):
    repository, *_ = collection
    albumin = run_query(repository, "labs", "albumin", parse_range(None, None))
    assert len(albumin.rows) + len(albumin.undated) >= 1
    nothing = run_query(repository, "labs", "zinc", parse_range(None, None))
    assert nothing.empty and "not in the dictionary" in " ".join(nothing.notes)


def test_query_requires_an_active_generation(repository, folders):
    with pytest.raises(QueryError, match="^NO_ACTIVE_GENERATION$"):
        run_query(repository, "labs", "hemoglobin", parse_range(None, None))


def test_cli_labs_output_and_evidence_fact(collection, monkeypatch, capsys):
    repository, source, data = collection
    monkeypatch.setenv("FILEBROWNIE_SOURCE_DIR", str(source))
    monkeypatch.setenv("FILEBROWNIE_DATA_DIR", str(data))

    @contextmanager
    def local():
        yield repository

    monkeypatch.setattr(cli, "open_repository", local)
    assert cli.main(["labs", "hemoglobin", "--from", "2024", "--to", "2024"]) == 0
    output = capsys.readouterr().out
    assert "Laboratory history" in output and "Scan completed:" in output
    assert "dictionary revision" in output and "Candidates, not confirmed results" in output
    assert "no usable date: 1" in output
    assert "Possible unmatched mentions" in output
    assert "does not prove" in output
    line = next(item for item in output.splitlines() if "13.5" in item and "a.pdf" in item)
    reference = line.split()[-1]
    assert cli.main(["evidence", "fact", reference]) == 0
    detail = capsys.readouterr().out
    assert "a.pdf page 1" in detail and '"Hemoglobin"' in detail and "verified" in detail
    assert cli.main(["evidence", "fact", "zzzzzzzz"]) == 2
    assert cli.main(["labs", "hemoglobin", "--from", "nonsense"]) == 2


def test_visits_flat_timeline_with_distinct_event_types(repository, folders):
    source, data = folders
    pdf_lines(
        source / "visit.pdf",
        [
            (20, 40, "Referral to urologist"),
            (20, 55, "Date: 05.04.2024"),
            (20, 120, "Consultation with urologist"),
            (20, 135, "Visit date: 20.04.2024"),
            (20, 220, "Recommended consultation with ophthalmologist"),
            (20, 300, "Urology clinic letterhead"),
        ],
    )
    vision = FakeVision(
        {
            "document_class": "visit_note",
            "events": [
                {
                    "specialty": "urologist",
                    "event_type": "referral",
                    "wording": "Referral to urologist",
                    "dates": [{"raw": "05.04.2024", "role": "event"}],
                },
                {
                    "specialty": "urologist",
                    "event_type": "encounter",
                    "wording": "Consultation with urologist",
                    "dates": [{"raw": "20.04.2024", "role": "event"}],
                },
                {
                    "specialty": "ophthalmologist",
                    "event_type": "other",
                    "recommendation": True,
                    "wording": "Recommended consultation with ophthalmologist",
                },
            ],
        }
    )
    run_full_scan(repository, source, data, None, vision)
    result = run_query(repository, "visits", "урология", parse_range(None, None))
    assert [row.fields["event"] for row in result.rows] == ["referral", "encounter"]
    assert [row.date_text for row in result.rows] == ["2024-04-05 (event)", "2024-04-20 (event)"]
    # The letterhead mention is surfaced, never turned into a visit row.
    assert any("letterhead" in mention.text.lower() for mention in result.mentions)
    eyes = run_query(repository, "visits", "eye care", parse_range(None, None))
    (row,) = eyes.undated
    assert row.fields["event"] == "other — recommendation"
    assert "evidence: direct" in row.markers
    filtered = run_query(repository, "visits", "urologist", parse_range("2024-04-10", None))
    assert [row.fields["event"] for row in filtered.rows] == ["encounter"]


def test_coverage_warnings_keep_partial_files_when_units_exist():
    class Reader:
        def unlisted_sources(self, generation_id):
            return []

        def file_problems(self, generation_id):
            return [
                {
                    "sources": ["huge.pdf"],
                    "format": "pdf",
                    "status": "partial",
                    "warnings": ["DOCUMENT_PAGE_LIMIT"],
                    "page_count": 201,
                    "unit_count": 200,
                }
            ]

        def coverage_units(self, generation_id):
            return []

        def incomplete_table_warnings(self, generation_id):
            return []

    class Repository:
        reader = Reader()

    lines = coverage_warnings(Repository(), None, include_tables=True)
    assert any("DOCUMENT_PAGE_LIMIT" in line and "units recorded: 200" in line for line in lines)


def test_mention_sweep_matches_phrase_across_adjacent_spans(repository, folders):
    source, data = folders
    pdf_lines(
        source / "esr.pdf",
        [(20, 40, "erythrocyte"), (120, 40, "sedimentation"), (240, 40, "rate")],
    )
    run_full_scan(repository, source, data, None, FakeVision({"lab_rows": []}))
    result = run_query(repository, "labs", "erythrocyte sedimentation rate", parse_range(None, None))
    assert any("erythrocyte" in mention.text.lower() for mention in result.mentions)
