ALTER TABLE raw_signals
    ADD COLUMN IF NOT EXISTS schema_version VARCHAR(32) NOT NULL DEFAULT '1.0';

ALTER TABLE raw_signals
    ADD COLUMN IF NOT EXISTS enrichment_queued_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS raw_signals_source_type_idx ON raw_signals (source_type);

CREATE TABLE IF NOT EXISTS signal_enrichments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    raw_signal_id UUID NOT NULL UNIQUE REFERENCES raw_signals(id) ON DELETE CASCADE,
    schema_version VARCHAR(32) NOT NULL DEFAULT '1.0',
    event_type TEXT NOT NULL CHECK (event_type IN ('opening', 'expansion', 'other')),
    nursery_name TEXT,
    operator_name TEXT,
    address TEXT,
    expected_opening_date DATE,
    capacity INTEGER CHECK (capacity IS NULL OR capacity >= 0),
    lifecycle_stage TEXT NOT NULL CHECK (lifecycle_stage IN (
        'DISCOVERED', 'PLANNING', 'APPROVED', 'FIT_OUT', 'RECRUITING',
        'REGISTRATION', 'OPENING_SOON', 'OPEN'
    )),
    confidence NUMERIC(5, 4) NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    extracted_facts JSONB NOT NULL DEFAULT '{}'::jsonb,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    review_status TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (review_status IN ('PENDING', 'APPROVED', 'REJECTED')),
    reviewed_by TEXT,
    reviewed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS signal_enrichments_review_status_idx
    ON signal_enrichments (review_status);

CREATE INDEX IF NOT EXISTS signal_enrichments_created_at_idx
    ON signal_enrichments (created_at DESC);
