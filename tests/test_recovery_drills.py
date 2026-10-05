"""Phase 2 drills: interruption, damaged cache, egress failure, and index preservation."""

import pytest
from support import FakeVision, pdf_lines

from filebrownie.evidence.network import NetworkIsolationError
from filebrownie.ingestion import scan as scan_module
from filebrownie.ingestion.scan import run_full_scan, scan_sources
from filebrownie.presentation import cli

pytestmark = pytest.mark.integration

ROWS = {"lab_rows": [{"label": "Ferritin", "value": "12"}]}


@pytest.fixture
def folders(tmp_path, repository):
    source, data = tmp_path / "sources", tmp_path / "data"
    source.mkdir()
    data.mkdir()
    repository.migrate()
    return source, data


def document(source, name, value):
    pdf_lines(source / name, [(20, 40, "Ferritin"), (200, 40, value)])


class InterruptingVision(FakeVision):
    def extract(self, image_png, *, lab_row_hints=None):
        self.calls += 1
        if self.calls == 1:  # a.pdf is cached, so this is b.pdf
            raise KeyboardInterrupt
        return {"lab_rows": [{"label": "Ferritin", "value": "12"}]}


def test_interrupted_rescan_keeps_the_active_index_and_resumes_from_cache(repository, folders):
    source, data = folders
    document(source, "a.pdf", "12")
    first = run_full_scan(repository, source, data, None, FakeVision(ROWS))
    assert first.activation.activated
    document(source, "b.pdf", "13")

    with pytest.raises(KeyboardInterrupt):
        run_full_scan(repository, source, data, None, InterruptingVision())
    assert repository.active_generation_id() == first.generation_id
    assert (
        repository.connection.execute(
            "SELECT count(*) AS n FROM lab_results WHERE generation_id = %s", (first.generation_id,)
        ).fetchone()["n"]
        == 1
    )

    vision = FakeVision(
        [{"lab_rows": [{"label": "Ferritin", "value": "13"}]}]
    )  # a.pdf's output is already cached, so only b.pdf reaches the model
    second = run_full_scan(repository, source, data, None, vision)
    assert second.activation.activated and vision.calls == 1
    assert repository.active_generation_id() == second.generation_id
    states = [item.state for item in repository.generations()]
    assert states.count("active") == 1 and "superseded" not in states


def test_damaged_cache_entries_are_recomputed_not_trusted(repository, folders):
    source, data = folders
    document(source, "a.pdf", "12")
    run_full_scan(repository, source, data, None, FakeVision(ROWS))
    repository.connection.execute("UPDATE step_cache SET payload = '{\"page\": 7}'::jsonb")
    vision = FakeVision(ROWS)
    outcome = run_full_scan(repository, source, data, None, vision)
    assert vision.calls == 1 and outcome.activation.activated
    fact = repository.connection.execute(
        "SELECT verification FROM lab_results WHERE generation_id = %s", (outcome.generation_id,)
    ).fetchone()
    assert fact["verification"] == "verified"


def test_reachable_internet_refuses_the_scan_before_any_state_changes(
    repository, folders, monkeypatch
):
    source, data = folders
    document(source, "a.pdf", "12")
    first = run_full_scan(repository, source, data, None, FakeVision(ROWS))

    def reachable():
        raise NetworkIsolationError("OUTBOUND_NETWORK_REACHABLE")

    monkeypatch.setattr(scan_module, "ensure_no_outbound", reachable)
    vision = FakeVision(ROWS)
    with pytest.raises(NetworkIsolationError, match="OUTBOUND_NETWORK_REACHABLE"):
        _, _ = scan_sources(repository, source, data, "scan", None, vision)
    assert vision.calls == 0
    assert len(repository.generations()) == 1
    assert repository.active_generation_id() == first.generation_id


def test_cli_scan_reports_unisolated_runtime_with_a_fixed_code(
    repository, folders, monkeypatch, capsys, tmp_path
):
    from contextlib import contextmanager

    source, data = folders
    document(source, "a.pdf", "12")
    monkeypatch.setenv("FILEBROWNIE_SOURCE_DIR", str(source))
    monkeypatch.setenv("FILEBROWNIE_DATA_DIR", str(data))

    @contextmanager
    def local_repository():
        yield repository

    def reachable():
        raise NetworkIsolationError("OUTBOUND_NETWORK_REACHABLE")

    monkeypatch.setattr(cli, "open_repository", local_repository)
    monkeypatch.setattr(cli, "TesseractOcr", lambda models: None)
    monkeypatch.setattr(cli, "LlamaVisionClient", lambda url, models: FakeVision(ROWS))
    monkeypatch.setattr(scan_module, "ensure_no_outbound", reachable)
    assert cli.main(["scan"]) == 2
    assert capsys.readouterr().err.strip() == "OUTBOUND_NETWORK_REACHABLE"
    assert not repository.generations()


def test_failed_rescan_never_replaces_the_previous_usable_index(repository, folders):
    source, data = folders
    document(source, "a.pdf", "12")
    first = run_full_scan(repository, source, data, None, FakeVision(ROWS))
    document(source, "b.pdf", "13")
    broken = run_full_scan(repository, source, data, None, None)  # model unavailable
    assert broken.activation is not None and not broken.activation.activated
    assert repository.active_generation_id() == first.generation_id
    assert cli.main(["status"]) == 0
