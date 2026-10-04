"""Deterministic terminology resolution at query time (D15-D17, D29, D43). No model runs."""

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from importlib.resources import files

from filebrownie.evidence.normalize import contains_phrase, stem, tokens

LANGUAGES = ("en", "ru", "uk")
Key = tuple[str, ...]


def stem_key(key: Key) -> Key:
    return tuple(stem(token) for token in key)


def label_key(label: str) -> Key:
    return tokens(label)


def lookup_keys(term: str) -> tuple[Key, ...]:
    """Normalized keys for a query term, including hyphenated group ids such as iron-panel."""
    keys = [label_key(term)]
    if "-" in term:
        keys.append(label_key(term.replace("-", " ")))
    return tuple(dict.fromkeys(keys))


def load_seed() -> tuple[dict, str]:
    text = files("filebrownie.query").joinpath("dictionary_seed.json").read_text()
    return json.loads(text), hashlib.sha256(text.encode()).hexdigest()


@dataclass(frozen=True)
class Scope:
    """What a query term means: concepts plus every synonym to sweep for."""

    term: str
    kind: str
    concepts: frozenset[str]
    group: str | None
    synonyms: tuple[Key, ...]
    raw: Key


@dataclass(frozen=True)
class LabelMatch:
    """How a stored raw label relates to a query scope.

    status: mapped (exact after normalization), inflected (inflection only), auto-mapped
    (unreviewed proposal), ambiguous (exact for several concepts), raw (typed label itself).
    """

    status: str
    concepts: frozenset[str]

    @property
    def confirmed(self) -> bool:
        return self.status in ("mapped", "raw")


_RANK = {"mapped": 0, "raw": 0, "inflected": 1, "auto-mapped": 2, "ambiguous": 3}


@dataclass
class Dictionary:
    revision: int
    names: dict[str, str] = field(default_factory=dict)
    kinds: dict[str, str] = field(default_factory=dict)
    terms: dict[str, set[Key]] = field(default_factory=dict)  # seed plus accepted
    groups: dict[str, tuple[str, str, set[Key], tuple[str, ...]]] = field(default_factory=dict)
    proposals: dict[Key, set[str]] = field(default_factory=dict)  # active generation only
    rejected: set[tuple[Key, str]] = field(default_factory=set)
    _exact: dict[Key, set[str]] = field(default_factory=dict, init=False)
    _stems: dict[Key, set[str]] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        for concept, keys in self.terms.items():
            for key in keys:
                self._exact.setdefault(key, set()).add(concept)
                self._stems.setdefault(stem_key(key), set()).add(concept)

    def resolve(self, term: str, kind: str) -> Scope:
        """Concepts and synonyms for a typed term; an unknown term still searches raw labels."""
        raw = label_key(term)
        keys = lookup_keys(term)
        concepts: set[str] = set()
        group_name = None
        normalized_id = term.casefold().replace("_", "-")
        for group_id, (group_kind, name, group_terms, members) in self.groups.items():
            if group_kind != kind:
                continue
            if group_id.casefold().replace("_", "-") == normalized_id:
                concepts |= set(members)
                group_name = name
            elif any(
                any(key == wanted or stem_key(key) == stem_key(wanted) for wanted in keys)
                for key in group_terms
            ):
                concepts |= set(members)
                group_name = name
        for concept, concept_keys in self.terms.items():
            if self.kinds[concept] == kind and any(
                any(key == wanted or stem_key(key) == stem_key(wanted) for wanted in keys)
                for key in concept_keys
            ):
                concepts.add(concept)
        for wanted in keys:
            concepts |= self.proposals.get(wanted, set())
            for proposal_key, proposal_concepts in self.proposals.items():
                if stem_key(proposal_key) == stem_key(wanted):
                    concepts |= proposal_concepts
        synonyms: set[Key] = set(keys)
        for concept in concepts:
            synonyms |= self.terms.get(concept, set())
        return Scope(term, kind, frozenset(concepts), group_name, tuple(sorted(synonyms)), raw)

    def match(self, label: str, scope: Scope) -> LabelMatch | None:
        """Classify a stored raw label for a scope; None when it does not belong."""
        key = label_key(label)
        if not key:
            return None
        best: dict[str, str] = {}
        exact_for = self._exact.get(key, set())
        for concept in scope.concepts:
            if (key, concept) in self.rejected:
                continue
            if concept in exact_for:
                status = "ambiguous" if len(exact_for) > 1 else "mapped"
            elif concept in self._stems.get(stem_key(key), set()):
                status = "inflected"
            elif concept in self.proposals.get(key, set()) or any(
                concept in found and stem_key(other) == stem_key(key)
                for other, found in self.proposals.items()
            ):
                status = "auto-mapped"
            else:
                continue
            best[concept] = status
        if best:
            status = min(best.values(), key=_RANK.__getitem__)
            return LabelMatch(status, frozenset(best))
        if not scope.concepts and scope.raw:
            if key == scope.raw:
                return LabelMatch("raw", frozenset())
            if stem_key(key) == stem_key(scope.raw):
                return LabelMatch("inflected", frozenset())
        return None

    def propose(self, label: str, kind: str) -> list[str]:
        """Concepts whose terms occur inside an otherwise unmapped label (reviewable proposals)."""
        key = label_key(label)
        if not key or any(self.kinds[c] == kind for c in self._exact.get(key, ())):
            return []
        stemmed = stem_key(key)
        found = []
        for concept, keys in self.terms.items():
            if self.kinds[concept] != kind or (key, concept) in self.rejected:
                continue
            if any(stem_key(term) == stemmed for term in keys):
                continue  # inflection-only matches are query-time candidates already
            if any(
                contains_phrase(key, term) or contains_phrase(stemmed, stem_key(term))
                for term in keys
            ):
                found.append(concept)
        return sorted(found)

    def describe(self, concept_id: str) -> str:
        return f"{self.names.get(concept_id, concept_id)} [{concept_id}]"


def build(
    seed: dict,
    revision: int,
    accepted: Iterable[tuple[Key, str]] = (),
    rejected: Iterable[tuple[Key, str]] = (),
    proposals: Iterable[tuple[Key, str]] = (),
) -> Dictionary:
    dictionary = Dictionary(revision)
    for section, kind in (("analytes", "analyte"), ("specialties", "specialty")):
        for item in seed[section]:
            dictionary.names[item["id"]] = item["name"]
            dictionary.kinds[item["id"]] = kind
            keys = {label_key(item["name"])}
            for language in LANGUAGES:
                keys |= {label_key(label) for label in item.get(language, ())}
            dictionary.terms[item["id"]] = {key for key in keys if key}
    for item in seed["groups"]:
        keys = {label_key(item["name"])}
        for language in LANGUAGES:
            keys |= {label_key(label) for label in item.get(language, ())}
        dictionary.groups[item["id"]] = (
            item["kind"],
            item["name"],
            {key for key in keys if key},
            tuple(item["members"]),
        )
    for key, concept in accepted:
        if concept in dictionary.terms:
            dictionary.terms[concept].add(key)
    for key, concept in proposals:
        dictionary.proposals.setdefault(key, set()).add(concept)
    dictionary.rejected = set(rejected)
    dictionary.__post_init__()
    return dictionary
