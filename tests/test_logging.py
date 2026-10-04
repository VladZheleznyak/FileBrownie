import os
import time
from contextlib import contextmanager

import pytest
from support import FakeVision, pdf_lines

from filebrownie import logs
from filebrownie.presentation import cli

SENTINEL = "ZEBRA-SENTINEL-7731"


def test_only_allowlisted_tokens_numbers_and_ids_are_written(tmp_path):
    assert logs.log_event(
        tmp_path,
        "scan",
        "scan_finished",
        units=3,
        duration_ms=12.3456,
        reason="AWAITING_ACTIVATION",
        generation="6f1c2f0e-1a2b-4c3d-8e9f-0a1b2c3d4e5f",
        activated=False,
        filename=f"{SENTINEL}.pdf",  # not an allowed field
        error=f"failed on {SENTINEL} at /sources/x.pdf",  # not a code-like token
    )
    text = (tmp_path / "logs" / logs.LOG_NAME).read_text()
    assert SENTINEL not in text and "/sources" not in text
    assert "units=3" in text and "duration_ms=12.346" in text and "activated=0" in text
    assert "dropped_fields=2" in text


def test_unknown_events_and_components_are_refused(tmp_path):
    assert not logs.log_event(tmp_path, "scan", "free_text", units=1)
    assert not logs.log_event(tmp_path, "../x", "scan_started")
    assert not (tmp_path / "logs").exists()


def test_rotation_keeps_retention_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(logs, "MAX_BYTES", 400)
    for number in range(200):
        logs.log_event(tmp_path, "scan", "scan_started", operation=f"op-{number}")
    files = sorted(path.name for path in (tmp_path / "logs").iterdir())
    assert files == [logs.LOG_NAME, *(f"{logs.LOG_NAME}.{i}" for i in range(1, logs.BACKUPS + 1))]
    assert all(path.stat().st_size <= 400 for path in (tmp_path / "logs").iterdir())


def test_old_rotated_files_expire(tmp_path, monkeypatch):
    monkeypatch.setattr(logs, "MAX_BYTES", 200)
    for number in range(30):
        logs.log_event(tmp_path, "scan", "scan_started", operation=f"op-{number}")
    old = tmp_path / "logs" / f"{logs.LOG_NAME}.1"
    long_ago = time.time() - logs.MAX_AGE_SECONDS - 100
    os.utime(old, (long_ago, long_ago))
    logs.log_event(tmp_path, "scan", "scan_started", operation="op-new")
    assert not old.exists() or old.stat().st_mtime > long_ago


def test_symlinked_log_location_is_never_followed(tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    (tmp_path / "logs").symlink_to(target)
    assert not logs.log_event(tmp_path, "scan", "scan_started")
    assert list(target.iterdir()) == []


def test_log_files_are_private(tmp_path):
    logs.log_event(tmp_path, "scan", "scan_started")
    assert (tmp_path / "logs" / logs.LOG_NAME).stat().st_mode & 0o077 == 0
    assert (tmp_path / "logs").stat().st_mode & 0o077 == 0


@pytest.mark.integration
def test_full_scan_and_failures_log_counts_without_names_and_erase_removes_them(
    repository, tmp_path, monkeypatch, capsys
):
    source, data = tmp_path / "sources", tmp_path / "data"
    source.mkdir()
    data.mkdir()
    pdf_lines(source / f"{SENTINEL}.pdf", [(20, 40, f"Ferritin {SENTINEL}"), (200, 40, "12")])
    monkeypatch.setenv("FILEBROWNIE_SOURCE_DIR", str(source))
    monkeypatch.setenv("FILEBROWNIE_DATA_DIR", str(data))
    monkeypatch.setenv("FILEBROWNIE_MODEL_DIR", str(tmp_path / "no-models"))

    @contextmanager
    def local_repository():
        yield repository

    monkeypatch.setattr(cli, "open_repository", local_repository)
    repository.migrate()
    assert cli.main(["scan"]) == 2  # models are not provisioned here
    text = (data / "logs" / logs.LOG_NAME).read_text()
    assert "event=operation_failed" in text and "error=MODELS_NOT_PROVISIONED" in text
    assert SENTINEL not in text and "no-models" not in text

    monkeypatch.setattr(cli, "TesseractOcr", lambda models: None)
    monkeypatch.setattr(cli, "LlamaVisionClient", lambda url, models: FakeVision({}))
    assert cli.main(["scan"]) == 0
    text = (data / "logs" / logs.LOG_NAME).read_text()
    assert "event=scan_started" in text and "event=scan_finished" in text
    assert "activated=1" in text and "units=1" in text
    assert SENTINEL not in text and ".pdf" not in text and str(tmp_path) not in text

    assert cli.main(["erase", "derived"]) == 0
    assert not list((data / "logs").iterdir())
