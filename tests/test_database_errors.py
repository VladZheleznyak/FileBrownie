import psycopg
import pytest

from filebrownie.storage import database
from filebrownie.storage.database import DatabaseError


def test_database_boundary_removes_raw_diagnostics(monkeypatch):
    def fail(*args, **kwargs):
        raise psycopg.OperationalError("synthetic-sensitive-db-diagnostic")

    monkeypatch.setattr(database.psycopg, "connect", fail)
    with pytest.raises(DatabaseError, match="^DATABASE_UNAVAILABLE$") as result:
        with database.open_repository():
            pytest.fail("Unexpected connection")
    assert "synthetic-sensitive" not in str(result.value)
