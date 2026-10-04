import hashlib
from contextlib import contextmanager

import pytest
from support import FakeVision, pdf_lines

from filebrownie.ingestion.scan import run_full_scan
from filebrownie.presentation import cli
from filebrownie.storage import erasure

pytestmark = pytest.mark.integration

LINES = [(20, 40, "Specimen collected: 12.03.2024"), (20, 80, "Ferritin"), (200, 80, "12")]
OUTPUT = {
    "lab_rows": [{"label": "Ferritin", "value": "12"}],
    "dates": [{"raw": "12.03.2024", "role": "specimen"}],
}


@pytest.fixture
def env(tmp_path, repository, monkeypatch):
    source, data = tmp_path / "sources", tmp_path / "data"
    source.mkdir()
    data.mkdir()
    monkeypatch.setenv("FILEBROWNIE_SOURCE_DIR", str(source))
    monkeypatch.setenv("FILEBROWNIE_DATA_DIR", str(data))

    @contextmanager
    def local_repository():
        yield repository

    monkeypatch.setattr(cli, "open_repository", local_repository)
    repository.migrate()
    return source, data


def scanned(repository, env, name="labs.pdf", lines=LINES):
    source, data = env
    pdf_lines(source / name, lines)
    return run_full_scan(repository, source, data, None, FakeVision(OUTPUT))


def fact_id(repository):
    return str(repository.connection.execute("SELECT id FROM lab_results").fetchone()["id"])


def record(repository, verdict="correct", note="matches the page"):
    return cli.main(
        ["check", "record", "--fact", fact_id(repository)[:8], "--verdict", verdict, "--note", note]
    )


def test_check_on_a_result_records_hash_location_verdict_and_note(repository, env, capsys):
    scanned(repository, env)
    assert record(repository) == 0
    (row,) = repository.connection.execute("SELECT * FROM manual_checks").fetchall()
    content = repository.connection.execute("SELECT content_hash FROM lab_results").fetchone()
    assert row["content_hash"] == content["content_hash"] and row["location"] == "page 1"
    assert row["verdict"] == "correct" and row["note"] == "matches the page"
    assert str(row["result_id"]) == fact_id(repository) and "Ferritin" in row["summary"]
    capsys.readouterr()
    assert cli.main(["check", "list"]) == 0
    assert "result and evidence available" in capsys.readouterr().out


def test_missed_item_needs_no_extracted_result(repository, env):
    source, _ = env
    scanned(repository, env)
    assert (
        cli.main(["check", "record", "--source", "labs.pdf", "--page", "1", "--verdict", "missed"])
        == 0
    )
    (row,) = repository.connection.execute("SELECT * FROM manual_checks").fetchall()
    assert row["result_id"] is None
    assert row["content_hash"] == hashlib.sha256((source / "labs.pdf").read_bytes()).hexdigest()


@pytest.mark.parametrize(
    "arguments",
    [
        ["--source", "labs.pdf", "--page", "1", "--verdict", "correct"],
        ["--source", "labs.pdf", "--verdict", "missed"],
        ["--source", "absent.pdf", "--page", "1", "--verdict", "missed"],
        ["--fact", "ffffffff", "--verdict", "correct"],
        ["--verdict", "correct"],
    ],
)
def test_invalid_check_references_are_rejected_with_codes(repository, env, arguments, capsys):
    scanned(repository, env)
    assert cli.main(["check", "record", *arguments]) == 2
    error = capsys.readouterr().err.strip()
    assert error and error.replace("_", "").isupper()
    assert (
        repository.connection.execute("SELECT count(*) AS n FROM manual_checks").fetchone()["n"]
        == 0
    )


def test_erase_derived_keeps_decisions_and_checks_and_labels_unavailable_evidence(
    repository, env, capsys
):
    source, data = env
    scanned(repository, env)
    record(repository, "wrong-value")
    repository.dictionary.decide("some label", "ferritin", "accepted")
    (data / "logs").mkdir()
    (data / "logs" / "app.log").write_text("diagnostic")
    original = (source / "labs.pdf").read_bytes()
    assert (data / "evidence").exists()

    assert cli.main(["erase", "derived"]) == 0
    tables = (
        "generations",
        "lab_results",
        "text_spans",
        "step_cache",
        "reader_cache",
        "contents",
        "term_proposals",
    )
    for table in tables:
        count = repository.connection.execute(f"SELECT count(*) AS n FROM {table}").fetchone()
        assert count["n"] == 0, table
    assert repository.active_generation_id() is None
    assert not list((data / "evidence").iterdir()) and not list((data / "logs").iterdir())
    assert (source / "labs.pdf").read_bytes() == original
    assert (
        repository.connection.execute("SELECT count(*) AS n FROM term_decisions").fetchone()["n"]
        == 1
    )
    capsys.readouterr()
    assert cli.main(["check", "list"]) == 0
    output = capsys.readouterr().out
    assert "generated evidence unavailable" in output and "wrong-value" in output
    assert cli.main(["labs", "ferritin"]) == 2  # nothing is queryable until a new scan


def test_checks_follow_content_not_paths(repository, env, capsys):
    source, _ = env
    scanned(repository, env)
    record(repository)
    cli.main(["erase", "derived"])
    # The same bytes rescanned produce new result ids, so the old check cannot reattach.
    run_full_scan(repository, source, env[1], None, FakeVision(OUTPUT))
    capsys.readouterr()
    cli.main(["check", "list"])
    assert "document present; generated result unavailable" in capsys.readouterr().out
    (source / "labs.pdf").unlink()
    scanned(repository, env, "renamed.pdf", [(20, 40, "Different content")])
    capsys.readouterr()
    cli.main(["check", "list"])
    assert "generated evidence unavailable" in capsys.readouterr().out


def test_erase_all_requires_the_typed_phrase(repository, env, monkeypatch, capsys):
    scanned(repository, env)
    record(repository)
    monkeypatch.setattr("builtins.input", lambda prompt: "yes")
    assert cli.main(["erase", "all"]) == 2
    assert "ERASE_CONFIRMATION_MISMATCH" in capsys.readouterr().err
    assert (
        repository.connection.execute("SELECT count(*) AS n FROM manual_checks").fetchone()["n"]
        == 1
    )
    assert repository.active_generation_id() is not None

    monkeypatch.setattr("builtins.input", lambda prompt: erasure.CONFIRMATION_PHRASE)
    assert cli.main(["erase", "all"]) == 0
    for table in ("generations", "manual_checks", "term_decisions", "lab_results"):
        count = repository.connection.execute(f"SELECT count(*) AS n FROM {table}").fetchone()
        assert count["n"] == 0, table
    assert repository.connection.execute("SELECT count(*) AS n FROM concepts").fetchone()["n"] > 0


def test_erase_without_a_terminal_does_not_confirm(repository, env, monkeypatch):
    scanned(repository, env)

    def closed(prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", closed)
    assert cli.main(["erase", "all"]) == 2
    assert repository.active_generation_id() is not None


def test_erase_derived_reports_cleanup_incomplete_and_retry_succeeds(
    repository, env, monkeypatch, capsys
):
    source, data = env
    scanned(repository, env)
    original_clear = erasure._clear_directory
    calls = {"n": 0}

    def flaky_clear(path, name):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("cleanup failed")
        return original_clear(path, name)

    monkeypatch.setattr(erasure, "_clear_directory", flaky_clear)
    assert cli.main(["erase", "derived"]) == 2
    assert "ERASE_CLEANUP_INCOMPLETE" in capsys.readouterr().err
    assert (
        repository.connection.execute("SELECT count(*) AS n FROM generations").fetchone()["n"] == 0
    )
    assert list((data / "evidence").iterdir())
    capsys.readouterr()
    assert cli.main(["erase", "derived"]) == 0
    assert not list((data / "evidence").iterdir())


def test_erase_refuses_a_symlinked_generated_folder_and_changes_nothing(
    repository, env, tmp_path, capsys
):
    source, data = env
    scanned(repository, env)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("not generated data")
    (data / "logs").symlink_to(outside)
    assert cli.main(["erase", "derived"]) == 2
    assert "ERASE_REFUSED_UNSAFE_PATH" in capsys.readouterr().err
    assert (outside / "keep.txt").exists()
    assert repository.active_generation_id() is not None


def test_check_summary_reports_counts_by_verdict_and_state_without_percentages(
    repository, env, capsys
):
    scanned(repository, env)
    assert cli.main(["check", "summary"]) == 0
    assert "nothing to evaluate" in capsys.readouterr().out
    record(repository, "wrong-value")
    cli.main(["check", "record", "--source", "labs.pdf", "--page", "1", "--verdict", "missed"])
    capsys.readouterr()
    assert cli.main(["check", "summary"]) == 0
    output = capsys.readouterr().out
    assert "wrong-value; reading was 'verified': 1" in output
    assert "missed; reading was 'evidence unavailable': 1" in output
    assert "%" not in output.replace("No percentage is given", "")
    cli.main(["erase", "derived"])
    capsys.readouterr()
    cli.main(["check", "summary"])
    assert "wrong-value; reading was 'evidence unavailable': 1" in capsys.readouterr().out
