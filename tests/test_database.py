from contextlib import contextmanager
from dataclasses import replace

import pytest
from psycopg.errors import CheckViolation

from filebrownie.ingestion.discovery import discover_sources
from filebrownie.presentation import cli
from filebrownie.storage.database import DatabaseError, migration_scripts

pytestmark = pytest.mark.integration


def test_migrations_are_idempotent_and_detect_modified_history(repository):
    repository.migrate()
    repository.migrate()
    repository.require_schema()
    assert repository.generations() == ()
    repository.connection.execute("UPDATE schema_migrations SET checksum = 'synthetic-changed'")
    with pytest.raises(DatabaseError, match="^MIGRATION_CHECKSUM_MISMATCH$"):
        repository.migrate()


def test_schema_required_before_use(repository):
    with pytest.raises(DatabaseError, match="^DATABASE_SCHEMA_REQUIRED"):
        repository.require_schema()


def test_saved_generation_roundtrips_with_shared_content(repository, tmp_path):
    repository.migrate()
    (tmp_path / "original.pdf").write_bytes(b"synthetic repeated")
    (tmp_path / "copy.JPG").write_bytes(b"synthetic repeated")
    (tmp_path / "unsupported.txt").write_bytes(b"synthetic unsupported")
    (tmp_path / "broken.pdf").symlink_to(tmp_path / "absent")
    inventory = discover_sources(tmp_path)
    generation_id = repository.begin_inventory()
    repository.save_inventory(generation_id, inventory)
    assert repository.load_inventory(generation_id) == inventory
    (generation,) = repository.generations()
    assert generation.state == "staged"
    assert generation.reason == "INVENTORY_ONLY"
    assert generation.source_count == 4
    assert generation.finished_at is not None
    row = repository.connection.execute("SELECT count(*) AS count FROM contents").fetchone()
    assert row["count"] == 2
    with pytest.raises(DatabaseError, match="^GENERATION_NOT_RUNNING$"):
        repository.save_inventory(generation_id, inventory)


def test_failed_save_rolls_back_sources_and_keeps_prior_generation(repository, tmp_path):
    repository.migrate()
    (tmp_path / "report.pdf").write_bytes(b"synthetic")
    inventory = discover_sources(tmp_path)
    previous = repository.begin_inventory()
    repository.save_inventory(previous, inventory)
    candidate = repository.begin_inventory()
    bad = replace(inventory.records[0], content_hash="synthetic-invalid-hash")
    with pytest.raises(CheckViolation):
        repository.save_inventory(candidate, replace(inventory, records=(bad,)))
    assert repository.load_inventory(previous) == inventory
    assert [item.state for item in repository.generations()] == ["staged", "running"]
    assert repository.recover_interrupted() == 1
    assert [item.state for item in repository.generations()] == ["staged", "interrupted"]
    assert repository.recover_interrupted() == 0
    with pytest.raises(DatabaseError, match="^GENERATION_INCOMPLETE$"):
        repository.load_inventory(candidate)


@pytest.mark.parametrize("change", ["modify", "add", "remove"])
def test_revalidation_invalidates_changed_sources_and_cannot_be_reset(repository, tmp_path, change):
    repository.migrate()
    source = tmp_path / "report.pdf"
    source.write_bytes(b"synthetic original")
    inventory = discover_sources(tmp_path)
    generation_id = repository.begin_inventory()
    repository.save_inventory(generation_id, inventory)
    assert repository.revalidate(generation_id, discover_sources(tmp_path)).consistent
    if change == "modify":
        source.write_bytes(b"synthetic changed")
    elif change == "add":
        (tmp_path / "added.jpeg").write_bytes(b"synthetic added")
    else:
        source.unlink()
    assert not repository.revalidate(generation_id, discover_sources(tmp_path)).consistent
    (generation,) = repository.generations()
    assert generation.state == "invalid"
    assert generation.reason == "SOURCE_CHANGED"
    source.write_bytes(b"synthetic original")
    (tmp_path / "added.jpeg").unlink(missing_ok=True)
    assert repository.revalidate(generation_id, discover_sources(tmp_path)).consistent
    assert repository.generations()[0].state == "invalid"


def test_cli_saved_inventory_and_validation(repository, tmp_path, monkeypatch, capsys):
    source, data = tmp_path / "sources", tmp_path / "data"
    source.mkdir()
    data.mkdir()
    (source / "report.pdf").write_bytes(b"synthetic")
    monkeypatch.setenv("FILEBROWNIE_SOURCE_DIR", str(source))
    monkeypatch.setenv("FILEBROWNIE_DATA_DIR", str(data))

    @contextmanager
    def local_repository():
        yield repository

    monkeypatch.setattr(cli, "open_repository", local_repository)
    assert cli.main(["migrate"]) == 0
    assert cli.main(["inventory", "--save"]) == 0
    generation_id = repository.generations()[0].id
    assert cli.main(["status", "--database"]) == 0
    assert cli.main(["validate", str(generation_id)]) == 0
    assert "staged" in capsys.readouterr().out
    (source / "report.pdf").write_bytes(b"synthetic changed")
    assert cli.main(["validate", str(generation_id)]) == 1
    output = capsys.readouterr().out
    assert "changed: 1" in output
    assert "content_hash" in output
    assert "Generation state: invalid" in output


def test_cli_interruption_leaves_recoverable_generation(repository, tmp_path, monkeypatch, capsys):
    source, data = tmp_path / "sources", tmp_path / "data"
    source.mkdir()
    data.mkdir()
    monkeypatch.setenv("FILEBROWNIE_SOURCE_DIR", str(source))
    monkeypatch.setenv("FILEBROWNIE_DATA_DIR", str(data))
    repository.migrate()

    @contextmanager
    def local_repository():
        yield repository

    def interrupt_discovery(root):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "open_repository", local_repository)
    monkeypatch.setattr(cli, "discover_sources", interrupt_discovery)
    assert cli.main(["inventory", "--save"]) == 130
    assert capsys.readouterr().err == "OPERATION_INTERRUPTED\n"
    assert repository.generations()[0].state == "interrupted"


def test_upgrade_from_first_schema_preserves_inventory(repository, tmp_path):
    from uuid import uuid4

    number, script, checksum = migration_scripts()[0]
    repository.connection.execute(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, checksum TEXT NOT NULL)"
    )
    repository.connection.execute(script)
    repository.connection.execute(
        "INSERT INTO schema_migrations VALUES (%s, %s)", (number, checksum)
    )
    identifier = uuid4()
    repository.connection.execute(
        "INSERT INTO generations (id, state, inventory_at, reason) "
        "VALUES (%s, 'staged', now(), 'INVENTORY_ONLY')",
        (identifier,),
    )
    repository.migrate()
    repository.require_schema()
    (generation,) = repository.generations()
    assert generation.id == identifier
    assert generation.kind == "inventory"
    assert generation.state == "staged"
    assert repository.load_inventory(identifier).records == ()
