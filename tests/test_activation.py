from contextlib import contextmanager

import pymupdf
import pytest
from PIL import Image

from filebrownie.ingestion.discovery import discover_sources
from filebrownie.ingestion.guard import (
    Outcome,
    SourceEntry,
    evaluate_replacement,
    has_usable_evidence,
)
from filebrownie.ingestion.scan import scan_sources
from filebrownie.presentation import cli
from filebrownie.storage.database import DatabaseError

pytestmark = pytest.mark.integration


@pytest.fixture
def folders(tmp_path, repository):
    source, data = tmp_path / "sources", tmp_path / "data"
    source.mkdir()
    data.mkdir()
    repository.migrate()
    return source, data


def pdf(path, label="Synthetic laboratory evidence"):
    with pymupdf.open() as document:
        page = document.new_page(width=300, height=200)
        page.insert_text((20, 40), label, fontsize=12)
        document.save(path)


def scan(repository, folders):
    source, data = folders
    return scan_sources(repository, source, data, kind="scan")


def test_inventory_and_reader_generations_cannot_activate(repository, folders):
    source, data = folders
    pdf(source / "a.pdf")
    reader = scan_sources(repository, source, data)
    with pytest.raises(DatabaseError, match="^GENERATION_NOT_ACTIVATABLE$"):
        repository.activate(reader, discover_sources(source))
    assert repository.active_generation_id() is None


def test_first_scan_activates_even_without_facts_and_shows_warnings(repository, folders):
    source, _ = folders
    pdf(source / "a.pdf")
    (source / "other.zip").write_bytes(b"synthetic archive")
    generation = scan(repository, folders)
    result = repository.activate(generation, discover_sources(source))
    assert result.activated and not result.findings
    assert repository.active_generation_id() == generation
    (item,) = repository.generations()
    assert item.state == "active" and item.activated_at is not None and not item.forced


def test_first_scan_without_usable_evidence_stays_staged_until_forced(repository, folders):
    source, _ = folders
    (source / "broken.pdf").write_bytes(b"synthetic malformed")
    generation = scan(repository, folders)
    result = repository.activate(generation, discover_sources(source))
    assert not result.activated
    assert result.findings[0].kind == "no usable supported evidence"
    assert repository.generations()[0].state == "staged"
    assert repository.generations()[0].reason == "ACTIVATION_GUARD"
    forced = repository.activate(generation, discover_sources(source), force=True)
    assert forced.activated and repository.generations()[0].forced


def test_replacement_supersedes_and_prunes_previous_generation(repository, folders):
    source, _ = folders
    pdf(source / "a.pdf")
    first = scan(repository, folders)
    repository.activate(first, discover_sources(source))
    second = scan(repository, folders)
    result = repository.activate(second, discover_sources(source))
    assert result.activated and result.previous == first and result.pruned == 1
    assert [item.id for item in repository.generations()] == [second]
    assert repository.active_generation_id() == second


def test_removed_source_is_not_a_regression(repository, folders):
    source, _ = folders
    pdf(source / "a.pdf")
    pdf(source / "b.pdf", "Synthetic second document")
    repository.activate(scan(repository, folders), discover_sources(source))
    (source / "b.pdf").unlink()
    second = scan(repository, folders)
    assert repository.activate(second, discover_sources(source)).activated


def test_new_failed_or_unsupported_content_keeps_replacement_staged(repository, folders):
    source, _ = folders
    pdf(source / "a.pdf")
    first = scan(repository, folders)
    repository.activate(first, discover_sources(source))
    (source / "broken.pdf").write_bytes(b"synthetic malformed")
    (source / "notes.zip").write_bytes(b"synthetic archive")
    second = scan(repository, folders)
    result = repository.activate(second, discover_sources(source))
    assert not result.activated
    kinds = {item.kind for item in result.findings}
    assert kinds == {"new content not fully processed", "new unsupported or failed file"}
    assert repository.active_generation_id() == first
    assert next(item for item in repository.generations() if item.id == second).state == "staged"
    forced = repository.activate(second, discover_sources(source), force=True)
    assert forced.activated and forced.previous == first


@pytest.mark.parametrize("change", ["modify", "add", "remove"])
def test_forced_activation_after_source_change_is_blocked(repository, folders, change):
    source, _ = folders
    pdf(source / "a.pdf")
    pdf(source / "b.pdf", "Synthetic second document")
    first = scan(repository, folders)
    repository.activate(first, discover_sources(source))
    (source / "broken.pdf").write_bytes(b"synthetic malformed")
    second = scan(repository, folders)
    assert not repository.activate(second, discover_sources(source)).activated
    if change == "modify":
        pdf(source / "a.pdf", "Synthetic changed evidence")
    elif change == "add":
        (source / "added.zip").write_bytes(b"synthetic archive")
    else:
        (source / "b.pdf").unlink()
    with pytest.raises(DatabaseError, match="^SOURCE_CHANGED$"):
        repository.activate(second, discover_sources(source), force=True)
    states = {item.id: item.state for item in repository.generations()}
    assert states == {first: "active", second: "invalid"}
    assert repository.active_generation_id() == first
    # Restoring the bytes does not revive an invalid generation.
    with pytest.raises(DatabaseError, match="^GENERATION_INVALID$"):
        repository.activate(second, discover_sources(source), force=True)


def test_interrupted_and_running_generations_never_activate(repository, folders, monkeypatch):
    source, data = folders
    pdf(source / "a.pdf")
    running = repository.begin_inventory(kind="scan")
    with pytest.raises(DatabaseError, match="^GENERATION_NOT_COMPLETE$"):
        repository.activate(running, discover_sources(source), force=True)
    repository.recover_interrupted()
    with pytest.raises(DatabaseError, match="^GENERATION_INTERRUPTED$"):
        repository.activate(running, discover_sources(source), force=True)
    assert repository.active_generation_id() is None


def test_validate_does_not_invalidate_the_active_generation(repository, folders):
    source, _ = folders
    pdf(source / "a.pdf")
    first = scan(repository, folders)
    repository.activate(first, discover_sources(source))
    (source / "a.pdf").unlink()
    assert not repository.revalidate(first, discover_sources(source)).consistent
    assert repository.generations()[0].state == "active"


def test_activation_cli_status_and_force(repository, folders, monkeypatch, capsys):
    source, data = folders
    pdf(source / "a.pdf")
    monkeypatch.setenv("FILEBROWNIE_SOURCE_DIR", str(source))
    monkeypatch.setenv("FILEBROWNIE_DATA_DIR", str(data))

    @contextmanager
    def local_repository():
        yield repository

    monkeypatch.setattr(cli, "open_repository", local_repository)
    first = scan(repository, folders)
    assert cli.main(["activate", str(first)]) == 0
    (source / "broken.pdf").write_bytes(b"synthetic malformed")
    second = scan(repository, folders)
    assert cli.main(["activate", str(second)]) == 1
    assert cli.main(["status", "--database"]) == 0
    output = capsys.readouterr().out
    assert "Guard finding: new content not fully processed" in output
    assert cli.main(["activate", str(second), "--force"]) == 0
    assert cli.main(["activate", str(second)]) == 2
    capsys.readouterr()
    assert cli.main(["status", "--database"]) == 0
    assert "forced past the coverage guard" in capsys.readouterr().out


def test_guard_summarizes_coverage_warning_detail():
    detail = (
        "MISSING_CONTEXT (unit 1), TEXT_LAYER_COVERAGE_UNVERIFIED (unit 48), "
        "TEXT_LAYER_COVERAGE_UNVERIFIED (unit 49)"
    )
    assert cli._summarize_coverage_detail(detail) == (
        "MISSING_CONTEXT (1), TEXT_LAYER_COVERAGE_UNVERIFIED (2)"
    )


def test_guard_flags_worse_status_new_warnings_and_fewer_facts():
    key = ("a" * 64, "pdf")
    before = Outcome("completed", frozenset({"W1"}), 1, ("a.pdf",))
    after = Outcome("failed", frozenset({"W1", "W2"}), 1, ("a.pdf",))
    findings = evaluate_replacement({key: after}, {key: before}, {key: 1}, {key: 3}, (), ())
    assert {item.kind for item in findings} == {
        "new processing failure",
        "new coverage warning",
        "fewer extracted facts",
    }
    assert not evaluate_replacement({key: before}, {key: after}, {key: 3}, {key: 3}, (), ())
    entries = (SourceEntry("n.zip", "unsupported", None),)
    assert not evaluate_replacement({}, {}, {}, {}, entries, entries)
    assert not has_usable_evidence({key: Outcome("failed", frozenset(), 0)})
    assert not has_usable_evidence({key: Outcome("completed", frozenset(), 0)})


def test_images_count_as_usable_partial_evidence(repository, folders):
    source, _ = folders
    Image.new("RGB", (40, 40), "white").save(source / "photo.jpg")
    generation = scan(repository, folders)
    assert repository.activate(generation, discover_sources(source)).activated
