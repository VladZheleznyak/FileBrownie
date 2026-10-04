ALTER TABLE generations ADD COLUMN kind TEXT NOT NULL DEFAULT 'inventory'
    CHECK (kind IN ('inventory', 'reader'));

CREATE TABLE reader_cache (
    cache_key TEXT PRIMARY KEY CHECK (cache_key ~ '^[0-9a-f]{64}$'),
    content_hash TEXT NOT NULL REFERENCES contents(content_hash),
    format TEXT NOT NULL CHECK (format IN ('pdf', 'jpeg')),
    configuration_hash TEXT NOT NULL CHECK (configuration_hash ~ '^[0-9a-f]{64}$'),
    reference UUID NOT NULL,
    artifacts JSONB NOT NULL
);

CREATE TABLE generation_reads (
    generation_id UUID NOT NULL REFERENCES generations(id) ON DELETE CASCADE,
    content_hash TEXT NOT NULL REFERENCES contents(content_hash),
    format TEXT NOT NULL CHECK (format IN ('pdf', 'jpeg')),
    reference UUID,
    status TEXT NOT NULL CHECK (status IN ('completed', 'partial', 'failed', 'skipped', 'unsupported')),
    warnings TEXT[] NOT NULL,
    page_count INTEGER CHECK (page_count >= 0),
    unit_count INTEGER NOT NULL CHECK (unit_count >= 0),
    cache_hit BOOLEAN NOT NULL,
    PRIMARY KEY (generation_id, content_hash, format)
);
