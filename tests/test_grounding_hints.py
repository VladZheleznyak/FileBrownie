from support import row

from filebrownie.interpretation.grounding import PageText, supplemental_lab_row_hints, vision_lab_row_hints


def test_supplemental_hint_for_bracketed_ferritin_ocr_row():
    spans = row(80, [(10, "320"), (80, "[FERRITIN")])
    page = PageText.build(spans)
    hints = supplemental_lab_row_hints(page, set())
    assert hints and "320" in hints[0] and "ferritin" in hints[0].casefold()


def test_vision_hints_merge_uncovered_and_supplemental():
    spans = row(80, [(10, "320"), (80, "[FERRITIN")])
    page = PageText.build(spans)
    assert vision_lab_row_hints(page, set())
