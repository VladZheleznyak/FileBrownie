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
from psycopg.types.json import Jsonb

from filebrownie.ingestion.consistency import InventoryDifference, compare_inventories
from filebrownie.ingestion.discovery import (
    DiscoveryStatus,
    SourceFormat,
    SourceInventory,
    SourceRecord,
)
from filebrownie.ingestion.guard import (
    ContentKey,
    Finding,
    Outcome,
    SourceEntry,
    evaluate_replacement,
    has_usable_evidence,
)
from filebrownie.storage.checks import CheckStore
from filebrownie.storage.dictionary import DictionaryStore
from filebrownie.storage.facts import FactReader, FactStore, UnitRecord


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
    kind: str
    activated_at: datetime | None = None
    forced: bool = False
    findings: tuple[Finding, ...] = ()


@dataclass(frozen=True)
class ActivationResult:
    activated: bool
    reason: str
    findings: tuple[Finding, ...]
    previous: UUID | None = None
    pruned: int = 0


@dataclass(frozen=True)
class GenerationRead:
    content_hash: str
    format: str
    reference: UUID | None
    status: str
    warnings: list[str]
    page_count: int | None
    unit_count: int
    cache_hit: bool
    sources: list[str]


def migration_scripts() -> tuple[tuple[int, str, str], ...]:
    scripts = []
    names = sorted(
        item.name
        for item in files("filebrownie.storage").joinpath("migrations").iterdir()
        if item.name.endswith(".sql")
    )
    for number, name in enumerate(names, 1):
        script = files("filebrownie.storage").joinpath("migrations", name).read_text()
        scripts.append((number, script, hashlib.sha256(script.encode()).hexdigest()))
    return tuple(scripts)


class Repository:
    """Call under the shared application lock. Transactions make each mutation atomic."""

    def __init__(self, connection: psycopg.Connection):
        self.connection = connection
        self.facts = FactStore(connection)
        self.reader = FactReader(connection)
        self.dictionary = DictionaryStore(connection)
        self.checks = CheckStore(connection)

    def migrate(self) -> None:
        scripts = migration_scripts()
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
            if any(row["version"] > len(scripts) for row in rows):
                raise DatabaseError("DATABASE_SCHEMA_NEWER")
            if [row["version"] for row in rows] != list(range(1, len(rows) + 1)):
                raise DatabaseError("DATABASE_SCHEMA_INCOMPATIBLE")
            for row, (_, _, checksum) in zip(rows, scripts, strict=False):
                if row["checksum"] != checksum:
                    raise DatabaseError("MIGRATION_CHECKSUM_MISMATCH")
            for number, script, checksum in scripts[len(rows) :]:
                self.connection.execute(script)
                self.connection.execute(
                    "INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)",
                    (number, checksum),
                )
        self.dictionary.sync_seed()

    def require_schema(self) -> None:
        row = self.connection.execute(
            "SELECT to_regclass('schema_migrations') AS relation"
        ).fetchone()
        if row["relation"] is None:
            raise DatabaseError("DATABASE_SCHEMA_REQUIRED: run filebrownie migrate")
        versions = self.connection.execute(
            "SELECT version, checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
        expected = [(number, checksum) for number, _, checksum in migration_scripts()]
        if [(row["version"], row["checksum"]) for row in versions] != expected:
            raise DatabaseError("DATABASE_SCHEMA_INCOMPATIBLE: run filebrownie migrate")

    def recover_interrupted(self) -> int:
        with self.connection.transaction():
            result = self.connection.execute(
                "UPDATE generations SET state = 'interrupted', finished_at = now(), "
                "reason = 'OPERATION_INTERRUPTED' WHERE state = 'running'"
            )
            return result.rowcount

    def begin_inventory(self, kind: str = "inventory") -> UUID:
        generation_id = uuid4()
        with self.connection.transaction():
            self.connection.execute(
                "INSERT INTO generations (id, state, reason, kind) "
                "VALUES (%s, 'running', 'DISCOVERY', %s)",
                (generation_id, kind),
            )
        return generation_id

    def save_inventory(
        self, generation_id: UUID, inventory: SourceInventory, finalize: bool = True
    ) -> None:
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
            if finalize:
                self.connection.execute(
                    "UPDATE generations SET state = 'staged', inventory_at = %s, "
                    "finished_at = now(), reason = 'INVENTORY_ONLY' WHERE id = %s",
                    (inventory.created_at, generation_id),
                )
            else:
                self.connection.execute(
                    "UPDATE generations SET inventory_at = %s, "
                    "reason = 'READER_PROCESSING' WHERE id = %s",
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
                    "UPDATE generations SET state = 'invalid', reason = %s "
                    "WHERE id = %s AND state IN ('running', 'staged')",
                    (reason, generation_id),
                )
        # A previously invalid generation stays invalid even if bytes are restored later.
        return difference

    def generations(self) -> tuple[Generation, ...]:
        rows = self.connection.execute(
            "SELECT g.id, g.state, g.started_at, g.finished_at, g.reason, g.kind, "
            "g.activated_at, g.forced, g.findings, count(s.relative_path) AS source_count "
            "FROM generations g LEFT JOIN generation_sources s ON s.generation_id = g.id "
            "GROUP BY g.id ORDER BY g.started_at, g.id"
        ).fetchall()
        return tuple(
            Generation(
                **{**row, "findings": tuple(Finding.from_json(item) for item in row["findings"])}
            )
            for row in rows
        )

    def cached_reader(self, cache_key: str) -> dict | None:
        return self.connection.execute(
            "SELECT * FROM reader_cache WHERE cache_key = %s", (cache_key,)
        ).fetchone()

    def cache_reader(
        self,
        cache_key: str,
        content_hash: str,
        format: str,
        configuration_hash: str,
        reference: str,
        artifacts: dict[str, str],
    ) -> None:
        with self.connection.transaction():
            self.connection.execute(
                "INSERT INTO reader_cache "
                "(cache_key, content_hash, format, configuration_hash, reference, artifacts) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (cache_key) DO UPDATE SET reference = EXCLUDED.reference, "
                "artifacts = EXCLUDED.artifacts",
                (
                    cache_key,
                    content_hash,
                    format,
                    configuration_hash,
                    UUID(reference),
                    Jsonb(artifacts),
                ),
            )

    def record_read(
        self,
        generation_id: UUID,
        content_hash: str,
        format: str,
        reference: str | None,
        status: str,
        warnings: list[str],
        page_count: int | None,
        unit_count: int,
        cache_hit: bool,
        units: tuple[UnitRecord, ...] = (),
    ) -> None:
        with self.connection.transaction():
            self._require_running(generation_id)
            source = self.connection.execute(
                "SELECT 1 FROM generation_sources "
                "WHERE generation_id = %s AND content_hash = %s AND format = %s LIMIT 1",
                (generation_id, content_hash, format),
            ).fetchone()
            if source is None:
                raise DatabaseError("GENERATION_SOURCE_NOT_FOUND")
            self.connection.execute(
                "INSERT INTO generation_reads "
                "(generation_id, content_hash, format, reference, status, warnings, "
                "page_count, unit_count, cache_hit) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    generation_id,
                    content_hash,
                    format,
                    UUID(reference) if reference else None,
                    status,
                    warnings,
                    page_count,
                    unit_count,
                    cache_hit,
                ),
            )
            self.facts.insert_units(generation_id, content_hash, format, units)

    def generation_reads(self, generation_id: UUID) -> tuple[GenerationRead, ...]:
        rows = self.connection.execute(
            "SELECT r.content_hash, r.format, r.reference, r.status, r.warnings, "
            "r.page_count, r.unit_count, r.cache_hit, "
            "array_agg(s.relative_path ORDER BY s.relative_path) AS sources "
            "FROM generation_reads r JOIN generation_sources s ON "
            "s.generation_id = r.generation_id AND s.content_hash = r.content_hash "
            "AND s.format = r.format WHERE r.generation_id = %s "
            "GROUP BY r.generation_id, r.content_hash, r.format ORDER BY r.content_hash, r.format",
            (generation_id,),
        ).fetchall()
        return tuple(GenerationRead(**row) for row in rows)

    def finish_scan(self, generation_id: UUID, current: SourceInventory) -> InventoryDifference:
        """Complete a reader or full scan as staged (or invalid); activation is separate."""
        difference = compare_inventories(self.load_inventory(generation_id), current)
        with self.connection.transaction():
            self._require_running(generation_id)
            row = self.connection.execute(
                "SELECT kind FROM generations WHERE id = %s", (generation_id,)
            ).fetchone()
            if row["kind"] not in ("reader", "scan"):
                raise DatabaseError("GENERATION_KIND_MISMATCH")
            coverage = self.connection.execute(
                "SELECT (SELECT count(*) FROM (SELECT DISTINCT content_hash, format "
                "FROM generation_sources WHERE generation_id = %s AND status = 'ready' "
                "AND content_hash IS NOT NULL) expected) AS expected, "
                "(SELECT count(*) FROM generation_reads WHERE generation_id = %s) AS recorded",
                (generation_id, generation_id),
            ).fetchone()
            if coverage["expected"] != coverage["recorded"]:
                raise DatabaseError("GENERATION_READERS_INCOMPLETE")
            reason = "READERS_ONLY" if row["kind"] == "reader" else "AWAITING_ACTIVATION"
            if not difference.consistent:
                reason = (
                    "SOURCE_CHANGED"
                    if (difference.added or difference.removed or difference.changed)
                    else "SOURCE_UNVERIFIABLE"
                )
            self.connection.execute(
                "UPDATE generations SET state = %s, reason = %s, finished_at = now() WHERE id = %s",
                ("staged" if difference.consistent else "invalid", reason, generation_id),
            )
        return difference

    def ensure_seed(self) -> None:
        """Pick up a changed committed seed (e.g. after an upgrade) before using the dictionary."""
        self.dictionary.sync_seed()

    def propose_mappings(self, generation_id: UUID) -> int:
        """Reviewable proposals for unmapped labels, owned by the generation (D15)."""
        dictionary = self.dictionary.snapshot(None)
        proposals = []
        for table, kind, column in (
            ("lab_results", "analyte", "raw_label"),
            ("specialty_events", "specialty", "raw_specialty"),
        ):
            labels = self.connection.execute(
                f"SELECT DISTINCT {column} AS label FROM {table} WHERE generation_id = %s",
                (generation_id,),
            ).fetchall()
            for row in labels:
                proposals += [(row["label"], c) for c in dictionary.propose(row["label"], kind)]
        self.dictionary.store_proposals(generation_id, proposals)
        return len(proposals)

    # Activation lifecycle (D8, D10, D33, D35, D42).

    def active_generation_id(self) -> UUID | None:
        row = self.connection.execute("SELECT generation_id FROM active_generation").fetchone()
        return row["generation_id"] if row else None

    def outcomes(self, generation_id: UUID) -> dict[ContentKey, Outcome]:
        """File outcomes. Unit-level warnings and statuses are kept distinguishable per unit."""
        units: dict[ContentKey, list[dict]] = {}
        for row in self.facts.unit_outcomes(generation_id):
            units.setdefault((row["content_hash"], row["format"]), []).append(row)
        result = {}
        for item in self.generation_reads(generation_id):
            key = (item.content_hash, item.format)
            warnings = set(item.warnings)
            if key in units:
                warnings = {f"{code} (file)" for code in item.warnings}
                for row in units[key]:
                    warnings |= {f"{code} (unit {row['unit_number']})" for code in row["warnings"]}
                    if row["status"] != "completed":
                        warnings.add(f"unit {row['unit_number']} {row['status']}")
            result[key] = Outcome(
                item.status, frozenset(warnings), item.unit_count, tuple(item.sources)
            )
        return result

    def source_entries(self, generation_id: UUID) -> tuple[SourceEntry, ...]:
        rows = self.connection.execute(
            "SELECT relative_path, status, content_hash FROM generation_sources "
            "WHERE generation_id = %s AND kind = 'file' ORDER BY relative_path",
            (generation_id,),
        ).fetchall()
        return tuple(SourceEntry(**row) for row in rows)

    def fact_counts(self, generation_id: UUID) -> dict[ContentKey, int]:
        return self.facts.fact_counts(generation_id)

    def verified_fact_counts(self, generation_id: UUID) -> dict[ContentKey, int]:
        return self.facts.verified_fact_counts(generation_id)

    def _guard_findings(self, generation_id: UUID) -> tuple[Finding, ...]:
        candidate = self.outcomes(generation_id)
        active_id = self.active_generation_id()
        if active_id is None:
            if has_usable_evidence(candidate):
                return ()
            return (
                Finding(
                    "no usable supported evidence",
                    (),
                    "first scan needs at least one readable supported unit",
                ),
            )
        return evaluate_replacement(
            candidate,
            self.outcomes(active_id),
            self.fact_counts(generation_id),
            self.fact_counts(active_id),
            self.source_entries(generation_id),
            self.source_entries(active_id),
            candidate_verified_facts=self.verified_fact_counts(generation_id),
            active_verified_facts=self.verified_fact_counts(active_id),
        )

    def activate(
        self, generation_id: UUID, current: SourceInventory, force: bool = False
    ) -> ActivationResult:
        """Validate sources at this moment, apply the guard, then switch the single pointer.

        `current` must be a fresh inventory taken while holding the application lock.
        Force overrides the coverage guard only, never interruption or source changes.
        """
        row = self.connection.execute(
            "SELECT state, kind FROM generations WHERE id = %s", (generation_id,)
        ).fetchone()
        if row is None:
            raise DatabaseError("GENERATION_NOT_FOUND")
        blocked = {
            "running": "GENERATION_NOT_COMPLETE",
            "interrupted": "GENERATION_INTERRUPTED",
            "invalid": "GENERATION_INVALID",
            "active": "GENERATION_ALREADY_ACTIVE",
            "superseded": "GENERATION_SUPERSEDED",
        }
        if row["state"] in blocked:
            raise DatabaseError(blocked[row["state"]])
        if row["kind"] != "scan":
            raise DatabaseError("GENERATION_NOT_ACTIVATABLE")
        difference = self.revalidate(generation_id, current)
        if not difference.consistent:
            changed = difference.added or difference.removed or difference.changed
            raise DatabaseError("SOURCE_CHANGED" if changed else "SOURCE_UNVERIFIABLE")
        findings = self._guard_findings(generation_id)
        if findings and not force:
            with self.connection.transaction():
                self.connection.execute(
                    "UPDATE generations SET reason = 'ACTIVATION_GUARD', findings = %s "
                    "WHERE id = %s AND state = 'staged'",
                    (Jsonb([item.as_json() for item in findings]), generation_id),
                )
            return ActivationResult(False, "ACTIVATION_GUARD", findings)
        with self.connection.transaction():
            self.connection.execute(
                "SELECT 1 FROM generations WHERE id = %s FOR UPDATE", (generation_id,)
            )
            previous = self.active_generation_id()
            if previous is not None:
                self.connection.execute(
                    "UPDATE generations SET state = 'superseded' WHERE id = %s", (previous,)
                )
            self.connection.execute(
                "UPDATE generations SET state = 'active', reason = 'ACTIVE', activated_at = now(), "
                "forced = %s, findings = %s WHERE id = %s AND state = 'staged'",
                (bool(findings), Jsonb([item.as_json() for item in findings]), generation_id),
            )
            self.connection.execute(
                "INSERT INTO active_generation (singleton, generation_id) VALUES (TRUE, %s) "
                "ON CONFLICT (singleton) DO UPDATE SET generation_id = EXCLUDED.generation_id, "
                "activated_at = now()",
                (generation_id,),
            )
        return ActivationResult(True, "ACTIVE", findings, previous, self.prune_superseded())

    def prune_superseded(self) -> int:
        """Remove superseded generations' rows; the active one is protected by a foreign key."""
        with self.connection.transaction():
            return self.connection.execute(
                "DELETE FROM generations WHERE state = 'superseded'"
            ).rowcount


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
