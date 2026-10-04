import io
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from filebrownie import provisioning
from filebrownie.evidence.normalize import normalize
from filebrownie.evidence.ocr import OcrError, TesseractOcr, merge_words

MODELS = Path("/models")
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

TSV = "\n".join(
    [
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext",
        "4\t1\t1\t1\t1\t0\t10\t10\t300\t20\t-1\t",
        "5\t1\t1\t1\t1\t1\t10\t10\t80\t20\t95\tHemoglobin",
        "5\t1\t1\t1\t1\t2\t96\t10\t30\t20\t90\tA1c",
        "5\t1\t1\t1\t1\t3\t400\t10\t40\t20\t93\t13.5",
        "5\t1\t1\t1\t1\t4\t500\t10\t0\t20\t-1\t",
        "5\t1\t1\t1\t2\t1\t10\t50\t60\t20\t80\tFerritin",
        "5\t1\t1\t1\t2\t2\t400\t50\t30\t20\t80\t12",
    ]
)


def test_words_merge_into_cell_like_spans_and_scale_back():
    spans = merge_words(TSV, scale=2)
    assert [span.text for span in spans] == ["Hemoglobin A1c", "13.5", "Ferritin", "12"]
    assert spans[0].bbox == (5.0, 5.0, 63.0, 15.0)
    assert all(span.reader == "tesseract" for span in spans)


def test_malformed_tsv_yields_no_spans():
    assert merge_words("garbage") == ()
    assert merge_words("level\tconf\n5\tnot-a-number") == ()


class Response:
    def __init__(self, data):
        self.stream = io.BytesIO(data)

    def read(self, size):
        return self.stream.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def fake_manifest(monkeypatch):
    import hashlib

    good = b"synthetic model bytes"
    entry = {
        "path": "tessdata/x.traineddata",
        "url": "https://example.invalid/x",
        "sha256": hashlib.sha256(good).hexdigest(),
        "size": len(good),
    }
    monkeypatch.setattr(provisioning, "component_files", lambda component: [entry])
    return good, entry


def test_processing_never_downloads_and_reports_setup_instructions(tmp_path, fake_manifest):
    with pytest.raises(provisioning.SetupError, match="MODELS_NOT_PROVISIONED"):
        provisioning.require(tmp_path, "tesseract")
    with pytest.raises(provisioning.SetupError, match="docker compose --profile setup"):
        TesseractOcr(tmp_path)
    assert not list(tmp_path.rglob("*"))


def test_provisioning_verifies_checksums_and_replaces_altered_files(tmp_path, fake_manifest):
    good, entry = fake_manifest
    changed = provisioning.provision(tmp_path, "tesseract", lambda url: Response(good))
    assert changed == [entry["path"]] and provisioning.verify(tmp_path, "tesseract") == []
    assert provisioning.provision(tmp_path, "tesseract", lambda url: Response(b"unused")) == []
    (tmp_path / entry["path"]).write_bytes(b"altered!!!!!!!!!!!!!!!")
    assert provisioning.verify(tmp_path, "tesseract") == [entry["path"]]


@pytest.mark.parametrize("payload", [b"short", b"synthetic model bytes but longer than declared"])
def test_download_mismatch_is_rejected_and_leaves_nothing(tmp_path, fake_manifest, payload):
    with pytest.raises(provisioning.SetupError, match="DOWNLOAD_"):
        provisioning.provision(tmp_path, "tesseract", lambda url: Response(payload))
    assert not list(tmp_path.rglob("*.traineddata*"))


needs_models = pytest.mark.skipif(
    provisioning.verify(MODELS, "tesseract"),
    reason="Run the setup profile to provision Tesseract language data.",
)


def render(lines):
    font = ImageFont.truetype(FONT, 34)
    image = Image.new("RGB", (1500, 80 * len(lines) + 40), "white")
    draw = ImageDraw.Draw(image)
    for index, line in enumerate(lines):
        draw.text((40, 30 + 80 * index), line, fill="black", font=font)
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


@needs_models
@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("Hemoglobin 13.5 g/dL", ["hemoglobin", "13.5"]),
        ("Гемоглобин 128 г/л", ["гемоглобин", "128"]),
        ("Феритин 45 нг/мл", ["феритин", "45"]),
        ("Загальний аналіз крові", ["загальний", "аналіз"]),
    ],
)
def test_synthetic_english_russian_ukrainian_lines_are_read_with_locations(line, expected):
    engine = TesseractOcr(MODELS)
    spans = engine.read(render([line]))
    text = normalize(" ".join(span.text for span in spans))
    for token in expected:
        assert token in text
    assert all(span.bbox and span.bbox[0] >= 0 and span.bbox[3] <= 200 for span in spans)
    assert "tesseract" in engine.version.lower()


@needs_models
def test_unreadable_image_is_a_safe_error():
    with pytest.raises(OcrError, match="^OCR_IMAGE_UNREADABLE$"):
        TesseractOcr(MODELS).read(b"not an image")


@needs_models
@pytest.mark.integration
def test_scanned_jpg_with_real_ocr_grounds_vision_claims(repository, tmp_path):
    from support import FakeVision

    from filebrownie.ingestion.scan import run_full_scan

    source, data = tmp_path / "sources", tmp_path / "data"
    source.mkdir()
    data.mkdir()
    repository.migrate()
    image = Image.open(io.BytesIO(render(["Ferritin          12        ng/mL"])))
    image.save(source / "scan.jpg", quality=95)
    vision = FakeVision({"lab_rows": [{"label": "Ferritin", "value": "12", "unit": "ng/mL"}]})
    outcome = run_full_scan(repository, source, data, TesseractOcr(MODELS), vision)
    rows = repository.connection.execute("SELECT * FROM lab_results").fetchall()
    assert [(row["raw_label"], row["verification"]) for row in rows] == [("Ferritin", "verified")]
    assert outcome.activation.activated
    assert rows[0]["evidence"]
