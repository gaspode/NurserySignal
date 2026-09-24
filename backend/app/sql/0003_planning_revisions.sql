CREATE TABLE IF NOT EXISTS raw_signal_revisions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    raw_signal_id UUID NOT NULL REFERENCES raw_signals(id) ON DELETE CASCADE,
    content_sha256 CHAR(64) NOT NULL,
    source_url TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    planning_status TEXT,
    decision TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    evidence_bucket TEXT NOT NULL,
    evidence_key TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (raw_signal_id, content_sha256)
);

CREATE INDEX IF NOT EXISTS raw_signal_revisions_signal_idx
    ON raw_signal_revisions (raw_signal_id, observed_at DESC);
