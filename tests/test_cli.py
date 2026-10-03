import pytest

from filebrownie.presentation.cli import main
from filebrownie.storage.operation import operation_lock


def test_status_does_not_imply_processing(capsys):
    assert main(["status"]) == 0
    assert "No scan, extraction, history, or database integration" in capsys.readouterr().out


@pytest.mark.parametrize("command", ["model-service", "model-setup"])
def test_model_placeholders_fail_explicitly(command, capsys):
    assert main([command]) == 2
    assert "SETUP_NOT_IMPLEMENTED" in capsys.readouterr().err


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
