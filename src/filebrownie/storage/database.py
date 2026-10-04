"""Transactional local inventory storage; no extracted evidence or active index yet."""

import hashlib
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from importlib.resources import files
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row

from filebrownie.ingestion.consistency import InventoryDifference, compare_inventories
from filebrownie.ingestion.discovery import (
    DiscoveryStatus,
    SourceFormat,
    SourceInventory,
    SourceRecord,
)


class DatabaseError(Exception):
    """Only fixed operational error codes may cross the storage boundary."""


@dataclass(frozen=True)
class Generation:
    id: UUID
    state: str
    started_at: datetime
    finished_at: datetime | None
    reason: str
    source_count: int


class Repository:
    """Call under the shared application lock. Transactions make each mutation atomic."""

    def __init__(self, connection: psycopg.Connection):
        self.connection = connection

    def migrate(self) -> None:
        script = files("filebrownie.storage").joinpath("migrations/001_inventory.sql").read_text()
        checksum = hashlib.sha256(script.encode()).hexdigest()
        with self.connection.transaction():
            # Guard migrations at the database level as well as the application lock.
            self.connection.execute("SELECT pg_advisory_xact_lock(47190231)")
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version INTEGER PRIMARY KEY, checksum TEXT NOT NULL)"
            )
            rows = self.connection.execute(
                "SELECT version, checksum FROM schema_migrations ORDER BY version"
            ).fetchall()
            if any(row["version"] != 1 for row in rows):
                raise DatabaseError("DATABASE_SCHEMA_NEWER")
            if rows:
                if rows[0]["checksum"] != checksum:
                    raise DatabaseError("MIGRATION_CHECKSUM_MISMATCH")
                return
            self.connection.execute(script)
            self.connection.execute(
                "INSERT INTO schema_migrations (version, checksum) VALUES (1, %s)", (checksum,)
            )

    def require_schema(self) -> None:
        row = self.connection.execute(
            "SELECT to_regclass('schema_migrations') AS relation"
        ).fetchone()
        if row["relation"] is None:
            raise DatabaseError("DATABASE_SCHEMA_REQUIRED: run filebrownie migrate")
        versions = self.connection.execute("SELECT version FROM schema_migrations").fetchall()
        if [row["version"] for row in versions] != [1]:
            raise DatabaseError("DATABASE_SCHEMA_INCOMPATIBLE")

    def recover_interrupted(self) -> int:
        with self.connection.transaction():
            result = self.connection.execute(
                "UPDATE generations SET state = 'interrupted', finished_at = now(), "
                "reason = 'OPERATION_INTERRUPTED' WHERE state = 'running'"
            )
            return result.rowcount

    def begin_inventory(self) -> UUID:
        generation_id = uuid4()
        with self.connection.transaction():
            self.connection.execute(
                "INSERT INTO generations (id, state, reason) VALUES (%s, 'running', 'DISCOVERY')",
                (generation_id,),
            )
        return generation_id

    def save_inventory(self, generation_id: UUID, inventory: SourceInventory) -> None:
        with self.connection.transaction():
            self._require_running(generation_id)
            with self.connection.cursor() as cursor:
                cursor.executemany(
                    "INSERT INTO contents (content_hash) VALUES (%s) ON CONFLICT DO NOTHING",
                    [(digest,) for digest in inventory.content_sources()],
                )
                cursor.executemany(
                    "INSERT INTO generation_sources "
                    "(generation_id, relative_path, kind, format, status, content_hash, warnings) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    [
                        (
                            generation_id,
                            record.relative_path,
                            record.kind,
                            record.format.value if record.format else None,
                            record.status.value,
                            record.content_hash,
                            list(record.warnings),
                        )
                        for record in inventory.records
                    ],
                )
            self.connection.execute(
                "UPDATE generations SET state = 'staged', inventory_at = %s, "
                "finished_at = now(), reason = 'INVENTORY_ONLY' WHERE id = %s",
                (inventory.created_at, generation_id),
            )

    def interrupt(self, generation_id: UUID) -> None:
        with self.connection.transaction():
            self.connection.execute(
                "UPDATE generations SET state = 'interrupted', finished_at = now(), "
                "reason = 'OPERATION_INTERRUPTED' WHERE id = %s AND state = 'running'",
                (generation_id,),
            )

    def _require_running(self, generation_id: UUID) -> None:
        row = self.connection.execute(
            "SELECT state FROM generations WHERE id = %s FOR UPDATE", (generation_id,)
        ).fetchone()
        if row is None:
            raise DatabaseError("GENERATION_NOT_FOUND")
        if row["state"] != "running":
            raise DatabaseError("GENERATION_NOT_RUNNING")

    def load_inventory(self, generation_id: UUID) -> SourceInventory:
        row = self.connection.execute(
            "SELECT inventory_at FROM generations WHERE id = %s", (generation_id,)
        ).fetchone()
        if row is None:
            raise DatabaseError("GENERATION_NOT_FOUND")
        if row["inventory_at"] is None:
            raise DatabaseError("GENERATION_INCOMPLETE")
        sources = self.connection.execute(
            "SELECT * FROM generation_sources WHERE generation_id = %s ORDER BY relative_path",
            (generation_id,),
        ).fetchall()
        return SourceInventory(
            row["inventory_at"],
            tuple(
                SourceRecord(
                    record["relative_path"],
                    record["kind"],
                    SourceFormat(record["format"]) if record["format"] else None,
                    DiscoveryStatus(record["status"]),
                    record["content_hash"],
                    tuple(record["warnings"]),
                )
                for record in sources
            ),
        )

    def revalidate(self, generation_id: UUID, current: SourceInventory) -> InventoryDifference:
        difference = compare_inventories(self.load_inventory(generation_id), current)
        if not difference.consistent:
            reason = (
                "SOURCE_CHANGED"
                if (difference.added or difference.removed or difference.changed)
                else "SOURCE_UNVERIFIABLE"
            )
            with self.connection.transaction():
                self.connection.execute(
                    "UPDATE generations SET state = 'invalid', reason = %s WHERE id = %s",
                    (reason, generation_id),
                )
        # A previously invalid generation stays invalid even if bytes are restored later.
        return difference

    def generations(self) -> tuple[Generation, ...]:
        rows = self.connection.execute(
            "SELECT g.id, g.state, g.started_at, g.finished_at, g.reason, "
            "count(s.relative_path) AS source_count "
            "FROM generations g LEFT JOIN generation_sources s ON s.generation_id = g.id "
            "GROUP BY g.id ORDER BY g.started_at, g.id"
        ).fetchall()
        return tuple(Generation(**row) for row in rows)


@contextmanager
def open_repository() -> Iterator[Repository]:
    url = os.environ.get("FILEBROWNIE_DATABASE_URL", "postgresql://filebrownie@db/filebrownie")
    try:
        with psycopg.connect(url, autocommit=True, row_factory=dict_row, connect_timeout=5) as conn:
            yield Repository(conn)
    except psycopg.Error:
        raise DatabaseError("DATABASE_UNAVAILABLE") from None
    except UnicodeError:
        raise DatabaseError("DATABASE_TEXT_ENCODING_UNSUPPORTED") from None
