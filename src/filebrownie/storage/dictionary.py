"""Persistence for the dictionary seed, durable decisions, and generation-owned proposals."""

from uuid import UUID

import psycopg

from filebrownie.query.dictionary import LANGUAGES, Dictionary, build, label_key, load_seed


def _joined(label: str) -> str:
    return " ".join(label_key(label))


class DictionaryStore:
    def __init__(self, connection: psycopg.Connection):
        self.connection = connection

    def sync_seed(self) -> bool:
        """Load the committed seed when it changed; returns True if the revision advanced."""
        seed, digest = load_seed()
        with self.connection.transaction():
            row = self.connection.execute(
                "SELECT seed_hash FROM dictionary_state FOR UPDATE"
            ).fetchone()
            if row is not None and row["seed_hash"] == digest:
                return False
            for table in ("group_members", "group_terms", "concept_groups", "concept_terms"):
                self.connection.execute(f"DELETE FROM {table}")
            self.connection.execute("DELETE FROM concepts")
            with self.connection.cursor() as cursor:
                for section, kind in (("analytes", "analyte"), ("specialties", "specialty")):
                    for item in seed[section]:
                        cursor.execute(
                            "INSERT INTO concepts (id, kind, name) VALUES (%s, %s, %s)",
                            (item["id"], kind, item["name"]),
                        )
                        for language in LANGUAGES:
                            cursor.executemany(
                                "INSERT INTO concept_terms "
                                "(concept_id, language, label, norm_label) "
                                "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                                [
                                    (item["id"], language, label, _joined(label))
                                    for label in item.get(language, ())
                                ],
                            )
                for item in seed["groups"]:
                    cursor.execute(
                        "INSERT INTO concept_groups (id, kind, name) VALUES (%s, %s, %s)",
                        (item["id"], item["kind"], item["name"]),
                    )
                    for language in LANGUAGES:
                        cursor.executemany(
                            "INSERT INTO group_terms (group_id, language, label, norm_label) "
                            "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                            [
                                (item["id"], language, label, _joined(label))
                                for label in item.get(language, ())
                            ],
                        )
                    cursor.executemany(
                        "INSERT INTO group_members (group_id, concept_id) VALUES (%s, %s)",
                        [(item["id"], member) for member in item["members"]],
                    )
            self.connection.execute(
                "INSERT INTO dictionary_state (singleton, seed_hash, revision) "
                "VALUES (TRUE, %s, 1) ON CONFLICT (singleton) DO UPDATE "
                "SET seed_hash = EXCLUDED.seed_hash, revision = dictionary_state.revision + 1",
                (digest,),
            )
            return True

    def revision(self) -> int:
        row = self.connection.execute("SELECT revision FROM dictionary_state").fetchone()
        return row["revision"] if row else 0

    def _bump(self) -> None:
        self.connection.execute("UPDATE dictionary_state SET revision = revision + 1")

    def concept_exists(self, concept_id: str) -> bool:
        return (
            self.connection.execute(
                "SELECT 1 FROM concepts WHERE id = %s", (concept_id,)
            ).fetchone()
            is not None
        )

    def decide(self, label: str, concept_id: str, verdict: str) -> None:
        with self.connection.transaction():
            self.connection.execute(
                "INSERT INTO term_decisions (norm_label, concept_id, label, verdict) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (norm_label, concept_id) DO UPDATE "
                "SET verdict = EXCLUDED.verdict, decided_at = now()",
                (_joined(label), concept_id, label, verdict),
            )
            self._bump()

    def reverse(self, label: str, concept_id: str) -> bool:
        with self.connection.transaction():
            removed = self.connection.execute(
                "DELETE FROM term_decisions WHERE norm_label = %s AND concept_id = %s",
                (_joined(label), concept_id),
            ).rowcount
            if removed:
                self._bump()
            return bool(removed)

    def decisions(self) -> list[dict]:
        return self.connection.execute(
            "SELECT label, concept_id, verdict, decided_at FROM term_decisions "
            "ORDER BY decided_at, label"
        ).fetchall()

    def store_proposals(self, generation_id: UUID, proposals: list[tuple[str, str]]) -> None:
        """Replace this generation's proposals as (label, concept_id); it never touches others."""
        with self.connection.transaction():
            self.connection.execute(
                "DELETE FROM term_proposals WHERE generation_id = %s", (generation_id,)
            )
            with self.connection.cursor() as cursor:
                cursor.executemany(
                    "INSERT INTO term_proposals (generation_id, norm_label, label, concept_id) "
                    "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    [
                        (generation_id, _joined(label), label, concept)
                        for label, concept in proposals
                    ],
                )

    def proposals(self, generation_id: UUID) -> list[dict]:
        return self.connection.execute(
            "SELECT p.label, p.concept_id, p.norm_label, d.verdict FROM term_proposals p "
            "LEFT JOIN term_decisions d ON d.norm_label = p.norm_label "
            "AND d.concept_id = p.concept_id WHERE p.generation_id = %s "
            "ORDER BY p.label, p.concept_id",
            (generation_id,),
        ).fetchall()

    def snapshot(self, active_generation: UUID | None) -> Dictionary:
        """Seed, durable decisions, and only the active generation's proposals."""
        seed, _ = load_seed()
        rows = self.connection.execute(
            "SELECT norm_label, concept_id, verdict FROM term_decisions"
        ).fetchall()
        accepted = [
            (tuple(r["norm_label"].split()), r["concept_id"])
            for r in rows
            if r["verdict"] == "accepted"
        ]
        rejected = [
            (tuple(r["norm_label"].split()), r["concept_id"])
            for r in rows
            if r["verdict"] == "rejected"
        ]
        proposals = []
        if active_generation is not None:
            proposals = [
                (tuple(r["norm_label"].split()), r["concept_id"])
                for r in self.connection.execute(
                    "SELECT norm_label, concept_id FROM term_proposals WHERE generation_id = %s",
                    (active_generation,),
                ).fetchall()
            ]
        return build(seed, self.revision(), accepted, rejected, proposals)
