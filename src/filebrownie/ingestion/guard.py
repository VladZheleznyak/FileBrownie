"""Conservative replacement guard (D10, D35). Pure functions; no model or thresholds."""

from collections.abc import Mapping
from dataclasses import dataclass

# Higher is worse. Deliberately coarse: any worsening for unchanged content is flagged.
STATUS_RANK = {"completed": 0, "partial": 1, "unsupported": 2, "skipped": 2, "failed": 3}
USABLE_STATUSES = frozenset({"completed", "partial"})

ContentKey = tuple[str, str]  # (content_hash, format)


@dataclass(frozen=True)
class Outcome:
    """File-level processing outcome of one unique content in a generation."""

    status: str
    warnings: frozenset[str]
    unit_count: int = 0
    sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class SourceEntry:
    relative_path: str
    status: str
    content_hash: str | None


@dataclass(frozen=True)
class Finding:
    """A reason a replacement scan stays staged. References are intentional local output."""

    kind: str
    sources: tuple[str, ...]
    detail: str

    def as_json(self) -> dict:
        return {"kind": self.kind, "sources": list(self.sources), "detail": self.detail}

    @classmethod
    def from_json(cls, data: dict) -> "Finding":
        return cls(data["kind"], tuple(data["sources"]), data["detail"])


def has_usable_evidence(outcomes: Mapping[ContentKey, Outcome]) -> bool:
    return any(item.status in USABLE_STATUSES and item.unit_count > 0 for item in outcomes.values())


def evaluate_replacement(
    candidate: Mapping[ContentKey, Outcome],
    active: Mapping[ContentKey, Outcome],
    candidate_facts: Mapping[ContentKey, int],
    active_facts: Mapping[ContentKey, int],
    candidate_entries: tuple[SourceEntry, ...],
    active_entries: tuple[SourceEntry, ...],
) -> tuple[Finding, ...]:
    """Compare unchanged content at file granularity; removed sources are never regressions."""
    findings: list[Finding] = []
    for key, now in sorted(candidate.items()):
        before = active.get(key)
        if before is None:
            continue
        if STATUS_RANK.get(now.status, 3) > STATUS_RANK.get(before.status, 3):
            kind = "new processing failure" if now.status == "failed" else "worse processing status"
            findings.append(Finding(kind, now.sources, f"{before.status} -> {now.status}"))
        added = sorted(now.warnings - before.warnings)
        if added:
            findings.append(Finding("new coverage warning", now.sources, ", ".join(added)))
        was, is_ = active_facts.get(key, 0), candidate_facts.get(key, 0)
        if is_ < was:
            findings.append(Finding("fewer extracted facts", now.sources, f"{was} -> {is_}"))
    previous = {entry.relative_path: entry.status for entry in active_entries}
    for key, now in sorted(candidate.items()):
        if key in active:
            continue
        if now.status != "completed":
            findings.append(
                Finding("new content not fully processed", now.sources, f"status {now.status}")
            )
    for entry in candidate_entries:
        # Unsupported/failed/skipped entries have no read outcome; judge them by path.
        if entry.status != "ready" and previous.get(entry.relative_path) in (None, "ready"):
            findings.append(
                Finding("new unsupported or failed file", (entry.relative_path,), entry.status)
            )
    return tuple(findings)
