import pytest
from PIL import Image
from support import FakeVision, pdf_lines

from filebrownie.evidence.models import TextSpan
from filebrownie.evidence.ocr import OcrError
from filebrownie.ingestion.scan import run_full_scan, scan_sources
from filebrownie.interpretation.vision import VisionError

pytestmark = pytest.mark.integration

LAB_LINES = [
    (20, 40, "Specimen collected: 12.03.2024"),
    (20, 80, "Hemoglobin"),
    (200, 80, "13.5"),
    (280, 80, "g/dL"),
    (20, 100, "Ferritin"),
    (200, 100, "12"),
    (280, 100, "ng/mL"),
]
LAB_OUTPUT = {
    "document_class": "lab_report",
    "dates": [{"raw": "12.03.2024", "role": "specimen"}],
    "lab_rows": [
        {"label": "Hemoglobin", "value": "13.5", "unit": "g/dL"},
        {"label": "Ferritin", "value": "12", "unit": "ng/mL"},
    ],
}


@pytest.fixture
def folders(tmp_path, repository):
    source, data = tmp_path / "sources", tmp_path / "data"
    source.mkdir()
    data.mkdir()
    repository.migrate()
    return source, data


def facts(repository):
    return repository.connection.execute("SELECT * FROM lab_results ORDER BY raw_label").fetchall()


def test_full_scan_stores_verified_facts_text_and_activates(repository, folders):
    source, data = folders
    pdf_lines(source / "labs.pdf", LAB_LINES)
    vision = FakeVision(LAB_OUTPUT)
    outcome = run_full_scan(repository, source, data, None, vision)
    assert outcome.activation.activated
    assert repository.active_generation_id() == outcome.generation_id
    rows = facts(repository)
    assert [(row["raw_label"], row["verification"]) for row in rows] == [
        ("Ferritin", "verified"),
        ("Hemoglobin", "verified"),
    ]
    hemoglobin = rows[1]
    assert str(hemoglobin["value_number"]) == "13.5" and hemoglobin["unit"] == "g/dL"
    assert (
        str(hemoglobin["timeline_start"]) == "2024-03-12"
        and hemoglobin["timeline_role"] == "specimen"
    )
    assert hemoglobin["evidence"]
    spans = repository.connection.execute(
        "SELECT count(*) AS total FROM text_spans WHERE generation_id = %s",
        (outcome.generation_id,),
    ).fetchone()
    assert spans["total"] >= len(LAB_LINES)
    (read,) = repository.generation_reads(outcome.generation_id)
    assert read.status == "completed" and read.unit_count == 1


def test_second_scan_reuses_cached_vision_output_and_still_activates(repository, folders):
    source, data = folders
    pdf_lines(source / "labs.pdf", LAB_LINES)
    vision = FakeVision(LAB_OUTPUT)
    first = run_full_scan(repository, source, data, None, vision)
    second = run_full_scan(repository, source, data, None, vision)
    assert vision.calls == 1
    assert second.activation.activated and second.activation.previous == first.generation_id
    assert len(facts(repository)) == 2  # the superseded generation's facts were pruned


def test_changed_model_version_does_not_reuse_cached_output(repository, folders):
    source, data = folders
    pdf_lines(source / "labs.pdf", LAB_LINES)
    vision = FakeVision(LAB_OUTPUT)
    run_full_scan(repository, source, data, None, vision)
    vision.version = "fake-vision-2"
    run_full_scan(repository, source, data, None, vision)
    assert vision.calls == 2


def test_vision_failure_keeps_partial_evidence_and_blocks_regression(repository, folders):
    source, data = folders
    pdf_lines(source / "labs.pdf", LAB_LINES)
    good = run_full_scan(repository, source, data, None, FakeVision(LAB_OUTPUT))
    broken = FakeVision(VisionError("VISION_UNAVAILABLE"))
    broken.version = "fake-vision-broken"  # same version would legitimately reuse the cache
    outcome = run_full_scan(repository, source, data, None, broken)
    assert not outcome.activation.activated
    kinds = {item.kind for item in outcome.activation.findings}
    assert {"worse processing status", "new coverage warning", "fewer extracted facts"} <= kinds
    assert repository.active_generation_id() == good.generation_id
    (read,) = repository.generation_reads(outcome.generation_id)
    assert read.status == "partial"
    unit = repository.connection.execute(
        "SELECT status, warnings FROM unit_outcomes WHERE generation_id = %s",
        (outcome.generation_id,),
    ).fetchone()
    assert unit["status"] == "partial" and "VISION_UNAVAILABLE" in unit["warnings"]


def test_invalid_vision_output_is_reported_and_never_cached(repository, folders):
    source, data = folders
    pdf_lines(source / "labs.pdf", LAB_LINES)
    vision = FakeVision({"lab_rows": "not a list"})
    outcome = run_full_scan(repository, source, data, None, vision)
    unit = repository.connection.execute(
        "SELECT warnings FROM unit_outcomes WHERE generation_id = %s", (outcome.generation_id,)
    ).fetchone()
    assert "VISION_OUTPUT_INVALID" in unit["warnings"]
    assert (
        repository.connection.execute("SELECT count(*) AS n FROM step_cache").fetchone()["n"] == 0
    )


def test_image_without_ocr_is_partial_with_visible_setup_gap(repository, folders):
    source, data = folders
    Image.new("RGB", (60, 60), "white").save(source / "scan.jpg")
    outcome = run_full_scan(repository, source, data, None, FakeVision({}))
    unit = repository.connection.execute(
        "SELECT status, warnings, text_source FROM unit_outcomes WHERE generation_id = %s",
        (outcome.generation_id,),
    ).fetchone()
    assert unit["status"] == "partial" and "OCR_UNAVAILABLE" in unit["warnings"]
    assert unit["text_source"] == "none"
    assert outcome.activation.activated  # usable partial evidence on a first scan


class FakeOcr:
    version = "fake-ocr-1"

    def __init__(self, fail=False):
        self.calls, self.fail = 0, fail

    def read(self, image_png):
        self.calls += 1
        if self.fail:
            raise OcrError("OCR_FAILED")
        return (
            TextSpan("Ferritin", (10, 10, 90, 22), "fake-ocr"),
            TextSpan("12", (150, 10, 170, 22), "fake-ocr"),
        )


def test_scanned_image_uses_cached_ocr_and_becomes_verified(repository, folders):
    source, data = folders
    Image.new("RGB", (200, 60), "white").save(source / "scan.jpg")
    vision = FakeVision({"lab_rows": [{"label": "Ferritin", "value": "12"}]})
    ocr = FakeOcr()
    first = run_full_scan(repository, source, data, ocr, vision)
    run_full_scan(repository, source, data, ocr, vision)
    assert ocr.calls == 1 and vision.calls == 1
    unit = repository.connection.execute(
        "SELECT status, text_source, warnings FROM unit_outcomes ORDER BY generation_id LIMIT 1"
    ).fetchone()
    assert unit["status"] == "completed" and unit["text_source"] == "ocr"
    assert "OCR_REQUIRED" not in unit["warnings"]
    assert first.activation.activated
    assert facts(repository)[0]["verification"] == "verified"


def test_ocr_failure_is_visible_and_not_cached(repository, folders):
    source, data = folders
    Image.new("RGB", (200, 60), "white").save(source / "scan.jpg")
    outcome = run_full_scan(repository, source, data, FakeOcr(fail=True), FakeVision({}))
    unit = repository.connection.execute(
        "SELECT status, warnings FROM unit_outcomes WHERE generation_id = %s",
        (outcome.generation_id,),
    ).fetchone()
    assert unit["status"] == "partial" and "OCR_FAILED" in unit["warnings"]
    assert (
        repository.connection.execute(
            "SELECT count(*) AS n FROM step_cache WHERE step = 'ocr'"
        ).fetchone()["n"]
        == 0
    )


def test_reader_only_scans_do_not_run_interpretation(repository, folders):
    source, data = folders
    pdf_lines(source / "labs.pdf", LAB_LINES)
    generation, _difference = scan_sources(repository, source, data)
    assert facts(repository) == []
    assert repository.facts.unit_outcomes(generation) == []
