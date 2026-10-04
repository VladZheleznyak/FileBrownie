-- Multilingual dictionary: committed seed (synced), generation-owned proposals, durable decisions.
CREATE TABLE dictionary_state (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    seed_hash TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1)
);

CREATE TABLE concepts (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('analyte', 'specialty')),
    name TEXT NOT NULL
);

CREATE TABLE concept_terms (
    concept_id TEXT NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    language TEXT NOT NULL,
    label TEXT NOT NULL,
    norm_label TEXT NOT NULL,
    PRIMARY KEY (concept_id, norm_label)
);

CREATE TABLE concept_groups (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('analyte', 'specialty')),
    name TEXT NOT NULL
);

CREATE TABLE group_terms (
    group_id TEXT NOT NULL REFERENCES concept_groups(id) ON DELETE CASCADE,
    language TEXT NOT NULL,
    label TEXT NOT NULL,
    norm_label TEXT NOT NULL,
    PRIMARY KEY (group_id, norm_label)
);

CREATE TABLE group_members (
    group_id TEXT NOT NULL REFERENCES concept_groups(id) ON DELETE CASCADE,
    concept_id TEXT NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    PRIMARY KEY (group_id, concept_id)
);

-- Durable user decisions on label-to-concept pairs; they outlive scans and derived erasure.
CREATE TABLE term_decisions (
    norm_label TEXT NOT NULL,
    concept_id TEXT NOT NULL,
    label TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict IN ('accepted', 'rejected')),
    decided_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (norm_label, concept_id)
);

-- Derived, owned by a scan generation; only the active generation's rows join lookup.
CREATE TABLE term_proposals (
    generation_id UUID NOT NULL REFERENCES generations(id) ON DELETE CASCADE,
    norm_label TEXT NOT NULL,
    label TEXT NOT NULL,
    concept_id TEXT NOT NULL,
    PRIMARY KEY (generation_id, norm_label, concept_id)
);
