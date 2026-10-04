ALTER TABLE generations DROP CONSTRAINT generations_state_check;
ALTER TABLE generations ADD CONSTRAINT generations_state_check
    CHECK (state IN ('running', 'staged', 'interrupted', 'invalid', 'active', 'superseded'));

ALTER TABLE generations DROP CONSTRAINT generations_kind_check;
ALTER TABLE generations ADD CONSTRAINT generations_kind_check
    CHECK (kind IN ('inventory', 'reader', 'scan'));

ALTER TABLE generations ADD COLUMN activated_at TIMESTAMPTZ;
ALTER TABLE generations ADD COLUMN forced BOOLEAN NOT NULL DEFAULT FALSE;
-- Guard findings explaining why a completed scan stayed staged (references, not medical text).
ALTER TABLE generations ADD COLUMN findings JSONB NOT NULL DEFAULT '[]'::jsonb;

-- Exactly one row at most; queries read only the generation it points to.
CREATE TABLE active_generation (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    generation_id UUID NOT NULL REFERENCES generations(id) ON DELETE RESTRICT,
    activated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
