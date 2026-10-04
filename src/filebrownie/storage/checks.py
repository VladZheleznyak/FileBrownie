"""Manual acceptance checks (D24, D33). Local sensitive data; kept apart from derived rows."""

from uuid import UUID, uuid4

from psycopg import Connection

VERDICTS = ("correct", "wrong-value", "wrong-label", "wrong-date", "unsupported", "missed")
MAX_NOTE = 2000


class CheckStore:
    def __init__(self, connection: Connection):
        self.connection = connection

    def record(
        self,
        content_hash: str,
        location: str,
        verdict: str,
        note: str,
        result_id: UUID | None = None,
        summary: str | None = None,
    ) -> UUID:
        identifier = uuid4()
        self.connection.execute(
            "INSERT INTO manual_checks (id, content_hash, location, verdict, note, result_id, "
            "summary) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (identifier, content_hash, location, verdict, note[:MAX_NOTE], result_id, summary),
        )
        return identifier

    def listing(self, active_generation: UUID | None) -> list[dict]:
        """Checks with the availability of their generated evidence in the active index.

        Availability is decided by content hash and result id only; a path match never
        reattaches a check to changed content.
        """
        return self.connection.execute(
            """
            SELECT c.*,
              CASE
                WHEN c.result_id IS NOT NULL AND (
                  EXISTS (SELECT 1 FROM lab_results f WHERE f.id = c.result_id
                          AND f.generation_id = %(generation)s)
                  OR EXISTS (SELECT 1 FROM specialty_events f WHERE f.id = c.result_id
                             AND f.generation_id = %(generation)s)
                ) THEN 'result and evidence available'
                WHEN EXISTS (SELECT 1 FROM generation_sources s
                             WHERE s.generation_id = %(generation)s
                             AND s.content_hash = c.content_hash)
                  THEN 'document present; generated result unavailable'
                ELSE 'generated evidence unavailable'
              END AS availability
            FROM manual_checks c ORDER BY c.recorded_at, c.id
            """,
            {"generation": active_generation},
        ).fetchall()

    def counts(self) -> dict[str, int]:
        rows = self.connection.execute(
            "SELECT verdict, count(*) AS total FROM manual_checks GROUP BY verdict"
        ).fetchall()
        return {row["verdict"]: row["total"] for row in rows}

    def observed_by_state(self, active_generation: UUID | None) -> dict[tuple[str, str], int]:
        """Checked results by (verdict, verification state in the active index), counts only.

        Results whose generated rows are gone are grouped under 'evidence unavailable'.
        """
        rows = self.connection.execute(
            """
            SELECT c.verdict,
                   COALESCE(l.verification, e.verification, 'evidence unavailable') AS state,
                   count(*) AS total
            FROM manual_checks c
            LEFT JOIN lab_results l ON l.id = c.result_id AND l.generation_id = %(generation)s
            LEFT JOIN specialty_events e ON e.id = c.result_id
                 AND e.generation_id = %(generation)s
            GROUP BY 1, 2 ORDER BY 1, 2
            """,
            {"generation": active_generation},
        ).fetchall()
        return {(row["verdict"], row["state"]): row["total"] for row in rows}
