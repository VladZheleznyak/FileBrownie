"""Tesseract OCR with located text. Runs offline against provisioned language data (D40).

Output (and any diagnostics) stays in memory; nothing is written to logs. The image is
upscaled for recognition and coordinates are mapped back to the stored raster's pixels.
"""

import csv
import io
import os
import resource
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Protocol

from filebrownie.evidence.models import TextSpan
from filebrownie.provisioning import SetupError, component_version, require


class OcrError(Exception):
    """Fixed safe codes only; engine diagnostics never escape."""


class OcrEngine(Protocol):
    """`version` identifies engine, languages, data, and configuration for cache identity."""

    version: str

    def read(self, image_png: bytes) -> tuple[TextSpan, ...]: ...


LANGUAGES = "eng+rus+ukr"
TIMEOUT_SECONDS = 120
TARGET_LONG_EDGE = 2600
MAX_SCALE = 2
MAX_PIXELS = 40_000_000
PAGE_SEGMENTATION_MODE = "3"
GAP_FACTOR = 1.2  # words closer than this many line heights form one span


def _limits() -> None:
    resource.setrlimit(resource.RLIMIT_AS, (3 * 1024**3,) * 2)
    resource.setrlimit(resource.RLIMIT_CPU, (TIMEOUT_SECONDS,) * 2)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _tesseract_version(executable: str) -> str:
    try:
        result = subprocess.run(
            [executable, "--version"], capture_output=True, text=True, timeout=20, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        raise SetupError("OCR_ENGINE_UNAVAILABLE") from None
    text = (result.stdout or result.stderr).splitlines()
    return text[0].strip() if text else "unknown"


class TesseractOcr:
    def __init__(self, models_dir: Path, executable: str = "tesseract"):
        require(models_dir, "tesseract")
        if shutil.which(executable) is None:
            raise SetupError("OCR_ENGINE_UNAVAILABLE")
        self.executable = executable
        self.tessdata = models_dir / "tessdata"
        self.version = (
            f"{_tesseract_version(executable)};langs={LANGUAGES};psm={PAGE_SEGMENTATION_MODE};"
            f"data={component_version('tesseract')};scale={MAX_SCALE};gap={GAP_FACTOR}"
        )

    def read(self, image_png: bytes) -> tuple[TextSpan, ...]:
        from PIL import Image, ImageOps

        try:
            with Image.open(io.BytesIO(image_png)) as opened:
                image = ImageOps.autocontrast(opened.convert("L"))
        except Exception:
            raise OcrError("OCR_IMAGE_UNREADABLE") from None
        scale = 1
        if max(image.size) < TARGET_LONG_EDGE:
            scale = min(MAX_SCALE, max(1, TARGET_LONG_EDGE // max(image.size)))
        while image.width * image.height * scale * scale > MAX_PIXELS and scale > 1:
            scale -= 1
        if scale > 1:
            image = image.resize((image.width * scale, image.height * scale), Image.LANCZOS)
        environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "TESSDATA_PREFIX": str(self.tessdata),
            "OMP_THREAD_LIMIT": "4",
            "LC_ALL": "C.UTF-8",
        }
        with tempfile.TemporaryDirectory(prefix="ocr-") as directory:
            path = Path(directory) / "page.png"
            image.save(path)
            try:
                result = subprocess.run(
                    [
                        self.executable,
                        str(path),
                        "stdout",
                        "-l",
                        LANGUAGES,
                        "--oem",
                        "1",
                        "--psm",
                        PAGE_SEGMENTATION_MODE,
                        "-c",
                        "preserve_interword_spaces=1",
                        "-c",
                        "tessedit_create_tsv=1",
                    ],  # fmt: skip
                    capture_output=True,
                    timeout=TIMEOUT_SECONDS,
                    check=False,
                    env=environment,
                    preexec_fn=_limits,  # noqa: PLW1509
                )
            except (OSError, subprocess.TimeoutExpired):
                raise OcrError("OCR_FAILED") from None
        if result.returncode != 0:
            raise OcrError("OCR_FAILED")
        return merge_words(result.stdout.decode("utf-8", errors="replace"), scale)


def merge_words(tsv: str, scale: int = 1) -> tuple[TextSpan, ...]:
    """Group Tesseract words into cell-like spans: neighbours on one line with a small gap."""
    reader = csv.DictReader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE)
    lines: dict[tuple[str, str, str], list[tuple[float, float, float, float, str]]] = {}
    for row in reader:
        try:
            if row["level"] != "5" or float(row["conf"]) < 0 or not row["text"].strip():
                continue
            left, top = float(row["left"]), float(row["top"])
            box = (left, top, left + float(row["width"]), top + float(row["height"]))
        except (KeyError, TypeError, ValueError):
            continue
        key = (row["block_num"], row["par_num"], row["line_num"])
        lines.setdefault(key, []).append((*box, row["text"].strip()))
    spans = []
    for words in lines.values():
        words.sort(key=lambda word: word[0])
        group = [words[0]]
        for word in words[1:]:
            height = max(group[-1][3] - group[-1][1], word[3] - word[1])
            if word[0] - group[-1][2] <= GAP_FACTOR * height:
                group.append(word)
            else:
                spans.append(_span(group, scale))
                group = [word]
        spans.append(_span(group, scale))
    spans.sort(key=lambda span: (round((span.bbox[1] + span.bbox[3]) / 2 / 8), span.bbox[0]))
    return tuple(spans)


def _span(group: list[tuple[float, float, float, float, str]], scale: int) -> TextSpan:
    box = (
        min(word[0] for word in group) / scale,
        min(word[1] for word in group) / scale,
        max(word[2] for word in group) / scale,
        max(word[3] for word in group) / scale,
    )
    return TextSpan(" ".join(word[4] for word in group), box, "tesseract")
