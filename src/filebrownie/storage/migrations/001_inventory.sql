CREATE TABLE generations (
    id UUID PRIMARY KEY,
    state TEXT NOT NULL CHECK (state IN ('running', 'staged', 'interrupted', 'invalid')),
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    inventory_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    reason TEXT NOT NULL
);

CREATE TABLE contents (
    content_hash TEXT PRIMARY KEY CHECK (content_hash ~ '^[0-9a-f]{64}$')
);

CREATE TABLE generation_sources (
    generation_id UUID NOT NULL REFERENCES generations(id) ON DELETE CASCADE,
    relative_path TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('file', 'directory', 'symlink', 'special', 'unknown')),
    format TEXT CHECK (format IN ('pdf', 'jpeg')),
    status TEXT NOT NULL CHECK (status IN ('ready', 'unsupported', 'failed', 'skipped')),
    content_hash TEXT REFERENCES contents(content_hash),
    warnings TEXT[] NOT NULL,
    PRIMARY KEY (generation_id, relative_path)
);
