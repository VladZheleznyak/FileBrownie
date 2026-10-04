"""Synthetic helpers: documents are fabricated and carry no real data."""

import pymupdf

from filebrownie.evidence.models import TextSpan


class FakeVision:
    """Deterministic stand-in for the local vision model."""

    version = "fake-vision-1"

    def __init__(self, output=None):
        self.output = output if output is not None else {}
        self.calls = 0

    def extract(self, image_png: bytes) -> dict:
        self.calls += 1
        output = self.output
        if isinstance(output, list):  # one output per call, in document order
            output = output[self.calls - 1]
        if isinstance(output, Exception):
            raise output
        return output


def pdf_lines(path, lines, width=500, height=400):
    """Write a one-page PDF; each item is (x, y, text)."""
    with pymupdf.open() as document:
        page = document.new_page(width=width, height=height)
        for x, y, text in lines:
            page.insert_text((x, y), text, fontsize=11, fontname="helv")
        document.save(path)


def row(y, cells, height=10):
    """Located spans for one table row; cells are (x, text)."""
    return [TextSpan(text, (x, y, x + 8 * len(text), y + height)) for x, text in cells]
