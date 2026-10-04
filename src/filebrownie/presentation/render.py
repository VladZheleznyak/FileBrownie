"""English terminal tables for query results. Intentional local product output, never logged."""

import unicodedata

from filebrownie.query.history import HistoryResult, Row

LAB_COLUMNS = (
    ("Date (role)", "date"),
    ("Analyte as written", "label"),
    ("Value", "value"),
    ("Unit", "unit"),
    ("Reference", "reference"),
    ("Flag", "flag"),
    ("Specimen", "specimen"),
)
EVENT_COLUMNS = (
    ("Date (role)", "date"),
    ("Event", "event"),
    ("Specialty as written", "specialty"),
    ("Source wording", "wording"),
)


def safe(text: str) -> str:
    """Escape control and Unicode formatting characters; keep Cyrillic and Latin readable."""
    return "".join(
        character
        if not unicodedata.category(character).startswith("C")
        else character.encode("unicode_escape").decode("ascii")
        for character in text
    )


def table(headers: list[str], rows: list[list[str]]) -> list[str]:
    cells = [[safe(str(cell)) for cell in row] for row in rows]
    widths = [
        max([len(header)] + [len(row[index]) for row in cells])
        for index, header in enumerate(headers)
    ]
    line = "  ".join("-" * width for width in widths)
    out = ["  ".join(header.ljust(width) for header, width in zip(headers, widths, strict=True))]
    out.append(line)
    out += [
        "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip()
        for row in cells
    ]
    return out


def _cells(row: Row, columns) -> list[str]:
    return [row.date_text if key == "date" else row.fields[key] for _, key in columns]


def _row_table(result: HistoryResult, rows: list[Row], why: list[str] | None = None) -> list[str]:
    columns = LAB_COLUMNS if result.kind == "labs" else EVENT_COLUMNS
    headers = [name for name, _ in columns] + ["Markers"] + (["Why a candidate"] if why else [])
    headers += ["Source", "Ref"]
    body = [
        _cells(row, columns)
        + [", ".join(row.markers) or "-"]
        + ([why[index]] if why else [])
        + [row.sources, str(row.fact_id)[:8]]
        for index, row in enumerate(rows)
    ]
    out = table(headers, body)
    for row in rows:
        extras = [*row.notes, f"other dates: {row.other_dates}" if row.other_dates else ""]
        extras = [item for item in extras if item]
        if extras:
            out.append(f"  [{str(row.fact_id)[:8]}] {safe('; '.join(extras))}")
    return out


def render(result: HistoryResult) -> list[str]:
    title = "Laboratory history" if result.kind == "labs" else "Visit history"
    lines = [f"{title} for {safe(repr(result.term))}"]
    finished = result.scan_finished_at.isoformat() if result.scan_finished_at else "unknown"
    lines.append(
        f"Scan completed: {finished}; generation {str(result.generation_id)[:8]}; "
        f"dictionary revision {result.dictionary_revision}"
    )
    if result.window.active:
        start = result.window.start.isoformat() if result.window.start else "earliest"
        end = result.window.end.isoformat() if result.window.end else "latest"
        lines.append(f"Date filter: {start} to {end}")
    lines += [safe(note) for note in result.notes]
    lines.append("")
    if result.rows:
        lines.append(f"Dated results ({len(result.rows)}):")
        lines += _row_table(result, result.rows)
        lines.append("")
    if result.undated:
        lines.append(f"Undated results ({len(result.undated)}):")
        lines += _row_table(result, result.undated)
        lines.append("")
    if result.window.active:
        lines.append(
            f"Excluded from the date filter because they have no usable date: "
            f"{result.excluded_undated}"
        )
        lines.append("")
    if result.candidates:
        lines.append(
            f"Candidates, not confirmed results ({len(result.candidates)}): "
            "terminology, readings, or dates that need your review"
        )
        lines += _row_table(
            result,
            [item.row for item in result.candidates],
            ["; ".join(item.reasons) for item in result.candidates],
        )
        lines.append("")
    if result.mentions:
        lines.append(
            f"Possible unmatched mentions in source text ({len(result.mentions)}): "
            "no result above is based on them"
        )
        for mention in result.mentions[:30]:
            lines.append(f"  {safe(mention.sources)}: {safe(mention.text)}")
        if len(result.mentions) > 30:
            lines.append(f"  ... and {len(result.mentions) - 30} more")
        lines.append("")
    if result.empty:
        lines.append(
            "No matching evidence was found in the indexed collection. This does not show "
            "that nothing exists; check the warnings and mentions below."
        )
    if result.warnings:
        lines.append("Coverage warnings:")
        lines += [f"  {safe(item)}" for item in result.warnings]
    else:
        lines.append("No coverage warnings were recorded; this does not prove completeness.")
    lines.append(
        "Results describe evidence in the indexed collection. Source agreement does not "
        "prove clinical correctness, and extraction is not guaranteed complete."
    )
    return lines
