"""Persistence for per-unit outcomes, located text, facts, and the step cache."""

import hashlib
import json
from dataclasses import dataclass
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb

from filebrownie.evidence.models import TextSpan
from filebrownie.interpretation.grounding import clean
from filebrownie.interpretation.models import (
    EXTRACTOR_VERSION,
    EventFact,
    LabFact,
    ReportedDate,
    Timeline,
    UnitInterpretation,
)


@dataclass(frozen=True)
class UnitRecord:
    number: int
    status: str
    warnings: tuple[str, ...]
    text_source: str
    spans: tuple[TextSpan, ...] = ()
    interpretation: UnitInterpretation | None = None


def step_cache_key(
    step: str, content_hash: str, format: str, unit_number: int, input_hash: str, version: str
) -> str:
    """Identity covers the exact input bytes and every output-affecting version (D9)."""
    return hashlib.sha256(
        json.dumps(
            [step, content_hash, format, unit_number, input_hash, version], separators=(",", ":")
        ).encode()
    ).hexdigest()


def _timeline_columns(timeline: Timeline) -> tuple:
    envelope = timeline.envelope
    return (
        timeline.role,
        envelope[0].start if envelope else None,
        envelope[1].end if envelope else None,
        Jsonb([item.as_json() for item in timeline.alternatives]),
    )


def _dates(dates: tuple[ReportedDate, ...]) -> Jsonb:
    return Jsonb([item.as_json() for item in dates])


class FactStore:
    """Call inside the repository's transactions; it never opens one of its own for inserts."""

    def __init__(self, connection: psycopg.Connection):
        self.connection = connection

    def insert_units(
        self, generation_id: UUID, content_hash: str, format: str, units: tuple[UnitRecord, ...]
    ) -> None:
        key = (generation_id, content_hash, format)
        with self.connection.cursor() as cursor:
            for unit in units:
                interpretation = unit.interpretation
                cursor.execute(
                    "INSERT INTO unit_outcomes (generation_id, content_hash, format, unit_number, "
                    "status, warnings, text_source, document_class) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        *key,
                        unit.number,
                        unit.status,
                        list(unit.warnings),
                        unit.text_source,
                        interpretation.document_class if interpretation else None,
                    ),
                )
                cursor.executemany(
                    "INSERT INTO text_spans (generation_id, content_hash, format, unit_number, "
                    "span_index, text, norm_text, x0, y0, x1, y1, reader) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    [
                        (
                            *key,
                            unit.number,
                            index,
                            span.text,
                            clean(span.text),
                            *(span.bbox if span.bbox else (None,) * 4),
                            span.reader,
                        )
                        for index, span in enumerate(unit.spans)
                    ],
                )
                if interpretation is None:
                    continue
                cursor.executemany(
                    "INSERT INTO interpretation_warnings (generation_id, content_hash, format, "
                    "unit_number, code, evidence) VALUES (%s, %s, %s, %s, %s, %s)",
                    [
                        (*key, unit.number, item.code, Jsonb(list(item.evidence)))
                        for item in interpretation.warnings
                    ],
                )
                for fact in interpretation.lab_facts:
                    self._insert_lab(cursor, key, unit.number, fact)
                for event in interpretation.events:
                    self._insert_event(cursor, key, unit.number, interpretation, event)

    @staticmethod
    def _insert_lab(cursor, key, unit_number: int, fact: LabFact) -> None:
        cursor.execute(
            "INSERT INTO lab_results (id, generation_id, content_hash, format, unit_number, "
            "extractor_version, raw_label, raw_value, comparator, value_number, qualitative, unit, "
            "reference_interval, flag, specimen, timeline_role, timeline_start, timeline_end, "
            "timeline_alternatives, dates, verification, evidence, alternative_label, "
            "alternative_evidence, notes) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                uuid4(),
                *key,
                unit_number,
                EXTRACTOR_VERSION,
                fact.raw_label,
                fact.raw_value,
                fact.comparator,
                fact.value_number,
                fact.qualitative,
                fact.unit,
                fact.reference_interval,
                fact.flag,
                fact.specimen,
                *_timeline_columns(fact.timeline),
                _dates(fact.dates),
                fact.verification,
                Jsonb(list(fact.evidence)),
                fact.alternative_label,
                Jsonb(list(fact.alternative_evidence)),
                list(fact.notes),
            ),
        )

    @staticmethod
    def _insert_event(
        cursor, key, unit_number: int, interpretation: UnitInterpretation, event: EventFact
    ) -> None:
        cursor.execute(
            "INSERT INTO specialty_events (id, generation_id, content_hash, format, unit_number, "
            "extractor_version, raw_specialty, event_type, recommendation, wording, planned, "
            "strength, document_class, timeline_role, timeline_start, timeline_end, "
            "timeline_alternatives, dates, verification, evidence, alternative_event_type, "
            "alternative_evidence, notes) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                uuid4(),
                *key,
                unit_number,
                EXTRACTOR_VERSION,
                event.raw_specialty,
                event.event_type,
                event.recommendation,
                event.wording,
                event.planned,
                event.strength,
                interpretation.document_class,
                *_timeline_columns(event.timeline),
                _dates(event.dates),
                event.verification,
                Jsonb(list(event.evidence)),
                event.alternative_event_type,
                Jsonb(list(event.alternative_evidence)),
                list(event.notes),
            ),
        )

    def unit_outcomes(self, generation_id: UUID) -> list[dict]:
        return self.connection.execute(
            "SELECT content_hash, format, unit_number, status, warnings "
            "FROM unit_outcomes WHERE generation_id = %s",
            (generation_id,),
        ).fetchall()

    def fact_counts(self, generation_id: UUID) -> dict[tuple[str, str], int]:
        rows = self.connection.execute(
            "SELECT content_hash, format, count(*) AS total FROM ("
            "SELECT content_hash, format FROM lab_results WHERE generation_id = %s "
            "UNION ALL SELECT content_hash, format FROM specialty_events WHERE generation_id = %s"
            ") facts GROUP BY content_hash, format",
            (generation_id, generation_id),
        ).fetchall()
        return {(row["content_hash"], row["format"]): row["total"] for row in rows}

    def verified_fact_counts(self, generation_id: UUID) -> dict[tuple[str, str], int]:
        rows = self.connection.execute(
            "SELECT content_hash, format, count(*) AS total FROM ("
            "SELECT content_hash, format FROM lab_results "
            "WHERE generation_id = %s AND verification = 'verified' "
            "UNION ALL SELECT content_hash, format FROM specialty_events "
            "WHERE generation_id = %s AND verification = 'verified'"
            ") facts GROUP BY content_hash, format",
            (generation_id, generation_id),
        ).fetchall()
        return {(row["content_hash"], row["format"]): row["total"] for row in rows}

    def cached_step(self, cache_key: str) -> dict | None:
        row = self.connection.execute(
            "SELECT payload FROM step_cache WHERE cache_key = %s", (cache_key,)
        ).fetchone()
        return row["payload"] if row else None

    def cache_step(
        self,
        cache_key: str,
        step: str,
        content_hash: str,
        format: str,
        unit_number: int,
        version: str,
        payload: dict,
    ) -> None:
        self.connection.execute(
            "INSERT INTO step_cache (cache_key, step, content_hash, format, unit_number, "
            "version_hash, payload) VALUES (%s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (cache_key) DO UPDATE SET payload = EXCLUDED.payload, created_at = now()",
            (
                cache_key,
                step,
                content_hash,
                format,
                unit_number,
                hashlib.sha256(version.encode()).hexdigest(),
                Jsonb(payload),
            ),
        )


class FactReader:
    """Read-only access to one generation's facts and located text, for deterministic queries."""

    def __init__(self, connection: psycopg.Connection):
        self.connection = connection

    def _facts(self, table: str, generation_id: UUID) -> list[dict]:
        return self.connection.execute(
            f"SELECT f.*, ARRAY(SELECT s.relative_path FROM generation_sources s "
            f"WHERE s.generation_id = f.generation_id AND s.content_hash = f.content_hash "
            f"AND s.format = f.format ORDER BY s.relative_path) AS sources "
            f"FROM {table} f WHERE f.generation_id = %s",
            (generation_id,),
        ).fetchall()

    def lab_results(self, generation_id: UUID) -> list[dict]:
        return self._facts("lab_results", generation_id)

    def specialty_events(self, generation_id: UUID) -> list[dict]:
        return self._facts("specialty_events", generation_id)

    def find_fact(self, generation_id: UUID, prefix: str) -> tuple[str, dict] | None:
        """Look a fact up by (a unique prefix of) its id within one generation."""
        for table in ("lab_results", "specialty_events"):
            rows = self.connection.execute(
                f"SELECT f.*, ARRAY(SELECT s.relative_path FROM generation_sources s "
                f"WHERE s.generation_id = f.generation_id AND s.content_hash = f.content_hash "
                f"AND s.format = f.format ORDER BY s.relative_path) AS sources "
                f"FROM {table} f WHERE f.generation_id = %s AND f.id::text LIKE %s LIMIT 2",
                (generation_id, prefix.lower() + "%"),
            ).fetchall()
            if len(rows) == 1:
                return table, rows[0]
        return None

    def unit_spans(
        self, generation_id: UUID, content_hash: str, format: str, unit_number: int
    ) -> list[dict]:
        return self.connection.execute(
            "SELECT span_index, text, x0, y0, x1, y1, reader FROM text_spans "
            "WHERE generation_id = %s AND content_hash = %s AND format = %s AND unit_number = %s "
            "ORDER BY span_index",
            (generation_id, content_hash, format, unit_number),
        ).fetchall()

    def spans(
        self, generation_id: UUID, content_hash: str, format: str, unit: int, indices: list[int]
    ) -> list[dict]:
        return self.connection.execute(
            "SELECT span_index, text, x0, y0, x1, y1, reader FROM text_spans "
            "WHERE generation_id = %s AND content_hash = %s AND format = %s AND unit_number = %s "
            "AND span_index = ANY(%s) ORDER BY span_index",
            (generation_id, content_hash, format, unit, indices),
        ).fetchall()

    def candidate_spans(self, generation_id: UUID, fragments: list[str]) -> list[dict]:
        """Spans whose normalized text contains any fragment; callers re-check precisely."""
        if not fragments:
            return []
        return self.connection.execute(
            "SELECT t.content_hash, t.format, t.unit_number, t.span_index, t.text, t.norm_text, "
            "ARRAY(SELECT s.relative_path FROM generation_sources s "
            "WHERE s.generation_id = t.generation_id AND s.content_hash = t.content_hash "
            "AND s.format = t.format ORDER BY s.relative_path) AS sources "
            "FROM text_spans t WHERE t.generation_id = %s AND t.norm_text LIKE ANY(%s)",
            (generation_id, [f"%{fragment}%" for fragment in fragments]),
        ).fetchall()

    def coverage_units(self, generation_id: UUID) -> list[dict]:
        """Units that are not fully processed or carry a flagged warning, with source paths."""
        return self.connection.execute(
            "SELECT u.content_hash, u.format, u.unit_number, u.status, u.warnings, "
            "ARRAY(SELECT s.relative_path FROM generation_sources s "
            "WHERE s.generation_id = u.generation_id AND s.content_hash = u.content_hash "
            "AND s.format = u.format ORDER BY s.relative_path) AS sources "
            "FROM unit_outcomes u WHERE u.generation_id = %s AND (u.status <> 'completed' "
            "OR u.warnings && ARRAY['HANDWRITING_NOT_INTERPRETED', 'MISSING_CONTEXT', "
            "'POSSIBLE_INCOMPLETE_TABLE_EXTRACTION', 'VISION_ITEMS_DROPPED', "
            "'TEXT_LAYER_COVERAGE_UNVERIFIED']) ORDER BY u.content_hash, u.unit_number",
            (generation_id,),
        ).fetchall()

    def incomplete_table_warnings(self, generation_id: UUID) -> list[dict]:
        return self.connection.execute(
            "SELECT w.content_hash, w.format, w.unit_number, w.evidence, "
            "ARRAY(SELECT s.relative_path FROM generation_sources s "
            "WHERE s.generation_id = w.generation_id AND s.content_hash = w.content_hash "
            "AND s.format = w.format ORDER BY s.relative_path) AS sources "
            "FROM interpretation_warnings w WHERE w.generation_id = %s "
            "AND w.code = 'POSSIBLE_INCOMPLETE_TABLE_EXTRACTION' "
            "ORDER BY w.content_hash, w.unit_number",
            (generation_id,),
        ).fetchall()

    def file_problems(self, generation_id: UUID) -> list[dict]:
        """Whole-file outcomes that are not simply completed, and non-ready inventory entries."""
        reads = self.connection.execute(
            "SELECT r.content_hash, r.format, r.status, r.warnings, r.page_count, r.unit_count, "
            "ARRAY(SELECT s.relative_path FROM generation_sources s "
            "WHERE s.generation_id = r.generation_id AND s.content_hash = r.content_hash "
            "AND s.format = r.format ORDER BY s.relative_path) AS sources "
            "FROM generation_reads r WHERE r.generation_id = %s AND r.status <> 'completed'",
            (generation_id,),
        ).fetchall()
        return reads

    def unlisted_sources(self, generation_id: UUID) -> list[dict]:
        return self.connection.execute(
            "SELECT relative_path, status, kind, warnings FROM generation_sources "
            "WHERE generation_id = %s AND status <> 'ready' ORDER BY relative_path",
            (generation_id,),
        ).fetchall()
