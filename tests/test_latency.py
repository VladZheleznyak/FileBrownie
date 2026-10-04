"""Representative query latency on synthetic data against the allowance (D18/D26)."""

import time

import pytest
from support import FakeVision, pdf_lines

from filebrownie.ingestion.scan import run_full_scan
from filebrownie.query.history import run_query
from filebrownie.query.timeline import parse_range

pytestmark = pytest.mark.integration

DOCUMENTS, ROWS_PER_DOCUMENT = 60, 25
ALLOWANCE_SECONDS = 300
ANALYTES = ["Hemoglobin", "Ferritin", "Glucose", "Creatinine", "Cholesterol"]


def test_group_and_analyte_queries_finish_far_inside_the_allowance(repository, tmp_path):
    source, data = tmp_path / "sources", tmp_path / "data"
    source.mkdir()
    data.mkdir()
    repository.migrate()
    outputs = []
    for number in range(DOCUMENTS):
        lines, rows = (
            [(20, 30, f"Specimen collected: {1 + number % 28:02d}.0{1 + number % 9}.2023")],
            [],
        )
        for row in range(ROWS_PER_DOCUMENT):
            label = f"{ANALYTES[row % len(ANALYTES)]} {row // len(ANALYTES) or ''}".strip()
            value = f"{number}.{row}"
            lines += [(20, 50 + 12 * row, label), (200, 50 + 12 * row, value)]
            rows.append({"label": label, "value": value})
        pdf_lines(source / f"doc-{number:03}.pdf", lines, height=400 + 12 * ROWS_PER_DOCUMENT)
        outputs.append({"lab_rows": rows, "dates": []})
    outcome = run_full_scan(repository, source, data, None, FakeVision(outputs))
    assert outcome.activation.activated
    total = repository.connection.execute("SELECT count(*) AS n FROM lab_results").fetchone()["n"]
    assert total >= DOCUMENTS * ROWS_PER_DOCUMENT * 0.9

    timings = {}
    for name, term, window in (
        ("analyte", "hemoglobin", parse_range(None, None)),
        ("group", "iron-panel", parse_range("2023-01", "2023-12")),
        ("sweep miss", "creatinine", parse_range(None, None)),
    ):
        started = time.monotonic()
        run_query(repository, "labs", term, window, dictionary=None)
        timings[name] = time.monotonic() - started
    print(f"\n{total} facts; query seconds: { {k: round(v, 2) for k, v in timings.items()} }")
    assert max(timings.values()) < ALLOWANCE_SECONDS / 10
