from filebrownie.ingestion.progress import ScanProgress
from filebrownie.presentation.cli import main
from filebrownie.presentation.progress import ScanEta, format_scan_progress
from filebrownie.storage.operation import operation_lock


def test_status_does_not_imply_processing(capsys):
    assert main(["status"]) == 0
    assert "check results against the source documents" in capsys.readouterr().out


def test_model_setup_verify_reports_missing_files_without_network(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FILEBROWNIE_MODEL_DIR", str(tmp_path))
    assert main(["model-setup", "--verify"]) == 1
    output = capsys.readouterr().out
    assert "needs setup" in output and "docker compose --profile setup" in output


def configure_inventory(tmp_path, monkeypatch):
    source, data = tmp_path / "sources", tmp_path / "data"
    source.mkdir()
    data.mkdir()
    monkeypatch.setenv("FILEBROWNIE_SOURCE_DIR", str(source))
    monkeypatch.setenv("FILEBROWNIE_DATA_DIR", str(data))
    return source, data


def test_inventory_output_reports_unsupported_and_escapes_controls(tmp_path, monkeypatch, capsys):
    source, _ = configure_inventory(tmp_path, monkeypatch)
    (source / "synthetic\nreport.PDF").write_bytes(b"synthetic")
    (source / "archive.zip").write_bytes(b"synthetic")
    assert main(["inventory"]) == 0
    output = capsys.readouterr().out
    assert "Supported files: 1" in output
    assert "Unsupported files: 1" in output
    assert "unsupported\t-\t'archive.zip'" in output
    assert "synthetic\\nreport.PDF" in output
    assert "extraction coverage is unknown" in output


def test_inventory_reports_busy_without_source_output(tmp_path, monkeypatch, capsys):
    _, data = configure_inventory(tmp_path, monkeypatch)
    with operation_lock(data):
        assert main(["inventory"]) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "OPERATION_BUSY\n"


def test_overlapping_directories_are_rejected(tmp_path, monkeypatch, capsys):
    source, _ = configure_inventory(tmp_path, monkeypatch)
    monkeypatch.setenv("FILEBROWNIE_DATA_DIR", str(source / "generated"))
    assert main(["inventory"]) == 2
    assert capsys.readouterr().err == "SOURCE_DATA_OVERLAP\n"


def test_skipped_source_makes_inventory_incomplete(tmp_path, monkeypatch, capsys):
    source, _ = configure_inventory(tmp_path, monkeypatch)
    (source / "broken.pdf").symlink_to(tmp_path / "absent")
    assert main(["inventory"]) == 1
    assert "SYMLINK_NOT_FOLLOWED" in capsys.readouterr().out


def test_scan_progress_lines_name_the_file_and_page_without_document_text():
    inventory = format_scan_progress(
        ScanProgress(stage="inventory", supported=2, unsupported=1, skipped=1, unique=1)
    )
    assert inventory == (
        "Supported files: 2; unsupported files: 1; skipped files: 1; unique contents to process: 1."
    )
    document = format_scan_progress(
        ScanProgress(
            stage="document",
            index=1,
            count=3,
            source="звіт\nlab.pdf",
            copies=2,
            format="pdf",
        )
    )
    assert document.startswith("[1/3] reading: ")
    assert "звіт" in document and "\\n" in document and "\n" not in document
    assert "plus 2 identical files" in document
    page = format_scan_progress(
        ScanProgress(stage="step", unit=2, pages=10, step="vision", cached=True)
    )
    assert page == "  page 2/10: vision (cached)"
    recorded = format_scan_progress(
        ScanProgress(
            stage="recorded",
            status="partial",
            warnings=("OCR_FAILED", "Ferritin 12"),
            elapsed_s=4,
        )
    )
    assert recorded == "  recorded partial; warnings: OCR_FAILED (4s)."


def test_eta_uses_measured_pdf_and_jpeg_rates_separately():
    clock = {"t": 0.0}

    def now() -> float:
        return clock["t"]

    eta = ScanEta(full_scan=True, clock=now)
    opening = format_scan_progress(
        eta.note(ScanProgress(stage="inventory", formats=("pdf", "jpeg")))
    )
    assert "ETA 30s (jpeg ~30s/page), plus 1 pdf of unknown length" in opening

    eta.note(ScanProgress(stage="document", index=1, count=2, source="a.pdf", format="pdf"))
    clock["t"] = 4
    opened = format_scan_progress(
        eta.note(ScanProgress(stage="step", unit=1, pages=2, step="vision", cached=False))
    )
    assert "unknown length" not in opened
    assert "jpeg ~30s/page" in opened
    assert "pdf ~" in opened

    clock["t"] = 24  # 20s of vision on page 1
    page_two = format_scan_progress(
        eta.note(ScanProgress(stage="step", unit=2, pages=2, step="vision", cached=False))
    )
    assert "pdf 22s/page" in page_two  # 4s reader / 2 pages + 20s vision
    assert "jpeg ~30s/page" in page_two
    clock["t"] = 44
    done = format_scan_progress(
        eta.note(
            ScanProgress(stage="recorded", status="completed", elapsed_s=44, format="pdf", pages=2)
        )
    )
    assert "ETA 30s (jpeg ~30s/page)" in done
    assert "pdf" not in done

    clock["t"] = 45
    jpeg_open = format_scan_progress(
        eta.note(ScanProgress(stage="document", index=2, count=2, source="scan.jpg", format="jpeg"))
    )
    assert "ETA 30s (jpeg ~30s/page)" in jpeg_open
    assert "22s" not in jpeg_open
    clock["t"] = 47
    eta.note(ScanProgress(stage="step", unit=1, pages=1, step="ocr", cached=False))
    clock["t"] = 77
    jpeg_done = format_scan_progress(
        eta.note(
            ScanProgress(stage="recorded", status="completed", elapsed_s=32, format="jpeg", pages=1)
        )
    )
    assert jpeg_done == "  recorded completed (32s)."


def test_cached_file_eta_drops_the_cold_page_prior():
    clock = {"t": 0.0}

    def now() -> float:
        return clock["t"]

    eta = ScanEta(full_scan=True, clock=now)
    eta.note(ScanProgress(stage="inventory", formats=("pdf", "pdf")))
    eta.note(
        ScanProgress(
            stage="document", index=1, count=2, source="a.pdf", format="pdf", cached=True, pages=2
        )
    )
    clock["t"] = 0.2
    eta.note(ScanProgress(stage="step", unit=1, pages=2, step="vision", cached=True))
    clock["t"] = 0.4
    eta.note(ScanProgress(stage="step", unit=2, pages=2, step="vision", cached=True))
    clock["t"] = 0.5
    eta.note(ScanProgress(stage="recorded", status="completed", elapsed_s=0, format="pdf", pages=2))
    following = format_scan_progress(
        eta.note(
            ScanProgress(
                stage="document",
                index=2,
                count=2,
                source="b.pdf",
                format="pdf",
                cached=True,
                pages=2,
            )
        )
    )
    assert "ETA 1s" in following
    assert "~30s" not in following


def test_full_scan_without_provisioned_models_reports_setup_instructions(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("FILEBROWNIE_MODEL_DIR", str(tmp_path))
    assert main(["scan"]) == 2
    error = capsys.readouterr().err
    assert "MODELS_NOT_PROVISIONED" in error and "model-setup" in error
