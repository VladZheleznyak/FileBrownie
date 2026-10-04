"""Synthetic end-to-end checks against the real local OCR and vision service.

Skipped unless models are provisioned and the inference service is running:
  docker compose --profile setup run --rm model-setup
  docker compose --profile inference up -d model
All pages are fabricated; no real medical data is involved.
"""

import io
import os
import time
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from filebrownie import provisioning
from filebrownie.evidence.ocr import TesseractOcr
from filebrownie.ingestion.scan import run_full_scan
from filebrownie.interpretation.llama_vision import LlamaVisionClient

MODELS = Path(os.environ.get("FILEBROWNIE_MODEL_DIR", "/models"))
URL = os.environ.get("FILEBROWNIE_MODEL_URL", "http://model:8080")
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

PAGES = {
    "en": (
        "Laboratory report",
        "Specimen collected: 12.03.2024",
        ("Test", "Result", "Unit", "Reference"),
        [
            ("Hemoglobin", "135", "g/L", "120-160"),
            ("Ferritin", "12", "ng/mL", "15-150"),
            ("Glucose", "5.4", "mmol/L", "3.9-5.9"),
        ],
    ),
    "ru": (
        "Лабораторное исследование",
        "Дата взятия материала: 12.03.2024",
        ("Показатель", "Результат", "Ед.", "Норма"),
        [
            ("Гемоглобин", "128", "г/л", "120-160"),
            ("Ферритин", "45", "нг/мл", "15-150"),
            ("Глюкоза", "5,4", "ммоль/л", "3,9-5,9"),
        ],
    ),
    "uk": (
        "Лабораторне дослідження",
        "Дата забору матеріалу: 12.03.2024",
        ("Показник", "Результат", "Од.", "Норма"),
        [
            ("Гемоглобін", "130", "г/л", "120-160"),
            ("Феритин", "18", "нг/мл", "15-150"),
            ("Глюкоза", "5,1", "ммоль/л", "3,9-5,9"),
        ],
    ),
}

ready = not (
    provisioning.verify(MODELS, "tesseract") or provisioning.verify(MODELS, "vision", False)
)
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not ready, reason="Models are not provisioned (run model-setup)."),
]


def render_page(title, date_line, header, rows) -> bytes:
    image = Image.new("RGB", (1240, 900), "white")
    draw = ImageDraw.Draw(image)
    heading, body = ImageFont.truetype(BOLD, 40), ImageFont.truetype(FONT, 30)
    draw.text((80, 60), title, fill="black", font=heading)
    draw.text((80, 140), date_line, fill="black", font=body)
    columns = (80, 460, 700, 900)
    for y, line in ((260, header), *((340 + 70 * i, row) for i, row in enumerate(rows))):
        for x, cell in zip(columns, line, strict=True):
            draw.text((x, y), cell, fill="black", font=heading if line is header else body)
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=92)
    return buffer.getvalue()


@pytest.mark.parametrize("language", sorted(PAGES))
def test_synthetic_scanned_lab_page_is_extracted_and_grounded(repository, tmp_path, language):
    source, data = tmp_path / "sources", tmp_path / "data"
    source.mkdir()
    data.mkdir()
    repository.migrate()
    title, date_line, header, rows = PAGES[language]
    (source / "scan.jpg").write_bytes(render_page(title, date_line, header, rows))
    client = LlamaVisionClient(URL, MODELS)
    started = time.monotonic()
    outcome = run_full_scan(repository, source, data, TesseractOcr(MODELS), client)
    elapsed = time.monotonic() - started
    stored = repository.connection.execute(
        "SELECT raw_label, raw_value, unit, verification, notes, specimen, timeline_start "
        "FROM lab_results"
    ).fetchall()
    print(f"\n[{language}] {elapsed:.1f}s for one page; facts: {len(stored)}")
    for unit in repository.facts.unit_outcomes(outcome.generation_id):
        print("    unit:", unit["status"], unit["warnings"])
    for fact in stored:
        print("   ", dict(fact))
    expected = {(label, value) for label, value, _unit, _ref in rows}
    found = {(fact["raw_label"], fact["raw_value"]) for fact in stored}
    assert outcome.activation is not None
    assert len(expected & found) >= 2, "vision output missed most synthetic rows"
    wrong = [
        fact
        for fact in stored
        if fact["verification"] == "verified"
        and (fact["raw_label"], fact["raw_value"]) not in expected
    ]
    assert not wrong, "a verified fact contradicts the synthetic page"


def test_cli_scan_activates_and_labs_query_answers_from_the_active_index(
    repository, tmp_path, monkeypatch, capsys
):
    from contextlib import contextmanager

    from filebrownie.presentation import cli

    source, data = tmp_path / "sources", tmp_path / "data"
    source.mkdir()
    data.mkdir()
    (source / "en.jpg").write_bytes(render_page(*PAGES["en"]))
    monkeypatch.setenv("FILEBROWNIE_SOURCE_DIR", str(source))
    monkeypatch.setenv("FILEBROWNIE_DATA_DIR", str(data))
    monkeypatch.setenv("FILEBROWNIE_MODEL_DIR", str(MODELS))
    monkeypatch.setenv("FILEBROWNIE_MODEL_URL", URL)

    @contextmanager
    def local_repository():
        yield repository

    monkeypatch.setattr(cli, "open_repository", local_repository)
    repository.migrate()
    assert cli.main(["scan"]) == 0
    assert "is now the active index" in capsys.readouterr().out
    assert cli.main(["labs", "ferritin"]) == 0
    output = capsys.readouterr().out
    assert "Ferritin" in output and "12" in output and "2024-03-12" in output


def degrade(kind: str, jpeg: bytes) -> bytes:
    import random

    from PIL import ImageEnhance, ImageFilter

    image = Image.open(io.BytesIO(jpeg)).convert("RGB")
    if kind == "skew":
        image = image.rotate(3, expand=True, fillcolor="white", resample=Image.BICUBIC)
    elif kind == "blur":
        image = image.filter(ImageFilter.GaussianBlur(1.6))
    elif kind == "low-contrast":
        image = ImageEnhance.Contrast(image).enhance(0.35)
    elif kind == "stamp":
        draw = ImageDraw.Draw(image)
        draw.ellipse((560, 420, 1000, 600), outline=(190, 30, 30), width=8)
        draw.text((640, 480), "RECEIVED", fill=(190, 30, 30), font=ImageFont.truetype(BOLD, 44))
    elif kind == "handwriting":
        draw, generator = ImageDraw.Draw(image), random.Random(7)
        x, y = 80, 700
        for _ in range(90):
            nx, ny = x + generator.randint(-25, 40), y + generator.randint(-30, 30)
            draw.line((x, y, nx, ny), fill=(20, 20, 120), width=3)
            x, y = max(60, min(1100, nx)), max(660, min(860, ny))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=85)
    return buffer.getvalue()


@pytest.mark.parametrize("kind", ["skew", "blur", "low-contrast", "stamp", "handwriting"])
def test_degraded_images_never_produce_verified_facts_that_contradict_the_page(
    repository, tmp_path, kind
):
    source, data = tmp_path / "sources", tmp_path / "data"
    source.mkdir()
    data.mkdir()
    repository.migrate()
    title, date_line, header, rows = PAGES["en"]
    (source / "scan.jpg").write_bytes(degrade(kind, render_page(title, date_line, header, rows)))
    run_full_scan(repository, source, data, TesseractOcr(MODELS), LlamaVisionClient(URL, MODELS))
    stored = repository.connection.execute(
        "SELECT raw_label, raw_value, verification FROM lab_results"
    ).fetchall()
    expected = {(label, value) for label, value, _unit, _ref in rows}
    verified = [fact for fact in stored if fact["verification"] == "verified"]
    print(f"\n[{kind}] facts: {len(stored)}, verified: {len(verified)} of {len(rows)} rows")
    contradicting = [
        fact for fact in verified if (fact["raw_label"], fact["raw_value"]) not in expected
    ]
    assert not contradicting
