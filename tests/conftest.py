import os
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row

from filebrownie.storage.database import Repository


@pytest.fixture
def repository():
    url = os.environ.get("FILEBROWNIE_TEST_DATABASE_URL")
    if url is None:
        pytest.skip("Use compose.test.yaml for the disposable database.")
    schema = "test_" + uuid4().hex
    with psycopg.connect(url, autocommit=True, row_factory=dict_row, connect_timeout=5) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        try:
            yield Repository(conn)
        finally:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
