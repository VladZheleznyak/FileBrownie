-- Manual acceptance checks (D24, D33): durable user decisions, independent of disposable
-- derived rows. No foreign keys to generations, facts, or contents on purpose, so pruning and
-- `erase derived` cannot delete them. Only `erase all` removes them.
CREATE TABLE manual_checks (
    id UUID PRIMARY KEY,
    content_hash TEXT NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    location TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK (
        verdict IN ('correct', 'wrong-value', 'wrong-label', 'wrong-date', 'unsupported', 'missed')
    ),
    note TEXT NOT NULL,
    result_id UUID,
    summary TEXT,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (result_id IS NOT NULL OR verdict = 'missed')
);
