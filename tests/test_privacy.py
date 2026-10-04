"""Leakage and read-only checks with sentinel strings (D38, D44). All data is synthetic."""

import hashlib
import logging
from contextlib import contextmanager

import pytest
from PIL import Image
from support import FakeVision, pdf_lines

from filebrownie.evidence.ocr import OcrError
from filebrownie.ingestion.scan import run_full_scan
from filebrownie.interpretation.vision import VisionError
from filebrownie.presentation import cli

pytestmark = pytest.mark.integration

SENTINEL = "ZEBRA-SENTINEL-7731"


class LeakyOcr:
    version = "leaky-ocr"

    def read(self, image_png):
        raise RuntimeError(f"engine crashed on {SENTINEL}")


class LeakyVision:
    version = "leaky-vision"

    def extract(self, image_png):
        raise ValueError(f"model said {SENTINEL}")


def snapshot(root):
    return sorted(
        (str(path.relative_to(root)), hashlib.sha256(path.read_bytes()).hexdigest())
        for path in root.rglob("*")
        if path.is_file()
    ) + sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_dir())


@pytest.fixture
def folders(tmp_path, repository, monkeypatch):
    source, data = tmp_path / "sources", tmp_path / "data"
    source.mkdir()
    data.mkdir()
    monkeypatch.setenv("FILEBROWNIE_SOURCE_DIR", str(source))
    monkeypatch.setenv("FILEBROWNIE_DATA_DIR", str(data))

    @contextmanager
    def local_repository():
        yield repository

    monkeypatch.setattr(cli, "open_repository", local_repository)
    repository.migrate()
    return source, data


def test_raw_engine_errors_never_reach_stored_codes_logs_or_stderr(
    repository, folders, caplog, capsys
):
    source, data = folders
    pdf_lines(source / f"{SENTINEL}.pdf", [(20, 40, f"Ferritin 12 {SENTINEL}")])
    Image.new("RGB", (60, 60), "white").save(source / f"{SENTINEL}-scan.jpg")
    caplog.set_level(logging.DEBUG)
    outcome = run_full_scan(repository, source, data, LeakyOcr(), LeakyVision())

    assert caplog.records == [] and SENTINEL not in capsys.readouterr().err
    warnings = repository.connection.execute(
        "SELECT warnings FROM unit_outcomes WHERE generation_id = %s", (outcome.generation_id,)
    ).fetchall()
    stored = [code for row in warnings for code in row["warnings"]]
    assert {"OCR_FAILED", "VISION_FAILED"} <= set(stored)
    assert all(SENTINEL not in code and code.replace("_", "").isupper() for code in stored)
    generation = repository.connection.execute("SELECT reason FROM generations").fetchone()
    assert SENTINEL not in generation["reason"]


def test_vision_and_ocr_error_types_expose_only_fixed_codes():
    for error in (VisionError("VISION_FAILED"), OcrError("OCR_FAILED")):
        assert str(error).isupper()


def test_sources_stay_byte_identical_through_scan_query_check_and_erase(
    repository, folders, monkeypatch
):
    source, data = folders
    pdf_lines(source / "labs.pdf", [(20, 40, "Ferritin"), (200, 40, "12")])
    Image.new("RGB", (80, 80), "white").save(source / "scan.jpg")
    before = snapshot(source)
    vision = FakeVision({"lab_rows": [{"label": "Ferritin", "value": "12"}]})
    run_full_scan(repository, source, data, None, vision)
    fact = str(repository.connection.execute("SELECT id FROM lab_results").fetchone()["id"])
    assert cli.main(["labs", "ferritin"]) == 0
    assert cli.main(["check", "record", "--fact", fact[:8], "--verdict", "correct"]) == 0
    assert (
        cli.main(["check", "record", "--source", "labs.pdf", "--page", "1", "--verdict", "missed"])
        == 0
    )
    assert cli.main(["erase", "derived"]) == 0
    monkeypatch.setattr("builtins.input", lambda prompt: "erase all")
    assert cli.main(["erase", "all"]) == 0
    assert snapshot(source) == before


def test_generated_data_stays_inside_the_evidence_folder(repository, folders):
    source, data = folders
    pdf_lines(source / "labs.pdf", [(20, 40, f"Ferritin 12 {SENTINEL}")])
    run_full_scan(repository, source, data, None, FakeVision({}))
    top_level = {path.name for path in data.iterdir()}
    assert top_level <= {"evidence"}
    assert SENTINEL not in " ".join(str(path) for path in data.rglob("*"))
