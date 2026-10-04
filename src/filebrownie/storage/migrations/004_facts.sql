-- Per-unit outcomes, located text, and typed medical facts, all owned by a scan generation.
CREATE TABLE unit_outcomes (
    generation_id UUID NOT NULL,
    content_hash TEXT NOT NULL,
    format TEXT NOT NULL,
    unit_number INTEGER NOT NULL CHECK (unit_number >= 1),
    status TEXT NOT NULL CHECK (status IN ('completed', 'partial', 'failed', 'skipped', 'unsupported')),
    warnings TEXT[] NOT NULL,
    text_source TEXT NOT NULL,
    document_class TEXT,
    PRIMARY KEY (generation_id, content_hash, format, unit_number),
    FOREIGN KEY (generation_id, content_hash, format)
        REFERENCES generation_reads (generation_id, content_hash, format) ON DELETE CASCADE
);

CREATE TABLE text_spans (
    generation_id UUID NOT NULL,
    content_hash TEXT NOT NULL,
    format TEXT NOT NULL,
    unit_number INTEGER NOT NULL,
    span_index INTEGER NOT NULL,
    text TEXT NOT NULL,
    norm_text TEXT NOT NULL,
    x0 DOUBLE PRECISION, y0 DOUBLE PRECISION, x1 DOUBLE PRECISION, y1 DOUBLE PRECISION,
    reader TEXT NOT NULL,
    PRIMARY KEY (generation_id, content_hash, format, unit_number, span_index),
    FOREIGN KEY (generation_id, content_hash, format, unit_number)
        REFERENCES unit_outcomes ON DELETE CASCADE
);

CREATE TABLE interpretation_warnings (
    generation_id UUID NOT NULL,
    content_hash TEXT NOT NULL,
    format TEXT NOT NULL,
    unit_number INTEGER NOT NULL,
    code TEXT NOT NULL,
    evidence JSONB NOT NULL,
    FOREIGN KEY (generation_id, content_hash, format, unit_number)
        REFERENCES unit_outcomes ON DELETE CASCADE
);

CREATE TABLE lab_results (
    id UUID PRIMARY KEY,
    generation_id UUID NOT NULL,
    content_hash TEXT NOT NULL,
    format TEXT NOT NULL,
    unit_number INTEGER NOT NULL,
    extractor_version TEXT NOT NULL,
    raw_label TEXT NOT NULL,
    raw_value TEXT NOT NULL,
    comparator TEXT,
    value_number NUMERIC,
    qualitative BOOLEAN NOT NULL,
    unit TEXT,
    reference_interval TEXT,
    flag TEXT,
    specimen TEXT,
    timeline_role TEXT,
    timeline_start DATE,
    timeline_end DATE,
    timeline_alternatives JSONB NOT NULL,
    dates JSONB NOT NULL,
    verification TEXT NOT NULL CHECK (verification IN ('verified', 'unverified reading', 'conflicting')),
    evidence JSONB NOT NULL,
    alternative_label TEXT,
    alternative_evidence JSONB NOT NULL,
    notes TEXT[] NOT NULL,
    FOREIGN KEY (generation_id, content_hash, format, unit_number)
        REFERENCES unit_outcomes ON DELETE CASCADE
);
CREATE INDEX lab_results_generation ON lab_results (generation_id);

CREATE TABLE specialty_events (
    id UUID PRIMARY KEY,
    generation_id UUID NOT NULL,
    content_hash TEXT NOT NULL,
    format TEXT NOT NULL,
    unit_number INTEGER NOT NULL,
    extractor_version TEXT NOT NULL,
    raw_specialty TEXT NOT NULL,
    event_type TEXT NOT NULL,
    recommendation BOOLEAN NOT NULL,
    wording TEXT NOT NULL,
    planned BOOLEAN NOT NULL,
    strength TEXT NOT NULL CHECK (strength IN ('direct', 'indirect', 'weak')),
    document_class TEXT NOT NULL,
    timeline_role TEXT,
    timeline_start DATE,
    timeline_end DATE,
    timeline_alternatives JSONB NOT NULL,
    dates JSONB NOT NULL,
    verification TEXT NOT NULL CHECK (verification IN ('verified', 'unverified reading', 'conflicting')),
    evidence JSONB NOT NULL,
    alternative_event_type TEXT,
    alternative_evidence JSONB NOT NULL,
    notes TEXT[] NOT NULL,
    FOREIGN KEY (generation_id, content_hash, format, unit_number)
        REFERENCES unit_outcomes ON DELETE CASCADE
);
CREATE INDEX specialty_events_generation ON specialty_events (generation_id);

-- Expensive model/OCR step outputs keyed by exact inputs and every output-affecting version.
CREATE TABLE step_cache (
    cache_key TEXT PRIMARY KEY CHECK (cache_key ~ '^[0-9a-f]{64}$'),
    step TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    format TEXT NOT NULL,
    unit_number INTEGER NOT NULL,
    version_hash TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
