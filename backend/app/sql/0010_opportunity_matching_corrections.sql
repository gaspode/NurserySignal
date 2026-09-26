ALTER TABLE opportunities ADD COLUMN IF NOT EXISTS vertical TEXT NOT NULL DEFAULT 'NURSERY';
ALTER TABLE opportunities ADD COLUMN IF NOT EXISTS operator_name TEXT;
ALTER TABLE opportunities ADD COLUMN IF NOT EXISTS address TEXT;
ALTER TABLE opportunities ADD COLUMN IF NOT EXISTS postcode TEXT;
ALTER TABLE opportunities ADD COLUMN IF NOT EXISTS town TEXT;
ALTER TABLE opportunities ADD COLUMN IF NOT EXISTS merged_into_opportunity_id UUID REFERENCES opportunities(id);

ALTER TABLE opportunity_signals ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'ACTIVE';
ALTER TABLE opportunity_signals ADD COLUMN IF NOT EXISTS created_by TEXT NOT NULL DEFAULT 'SYSTEM';
ALTER TABLE opportunity_signals ADD COLUMN IF NOT EXISTS match_outcome TEXT NOT NULL DEFAULT 'PROBABLE';
ALTER TABLE opportunity_signals ADD COLUMN IF NOT EXISTS match_confidence NUMERIC(5, 4);
ALTER TABLE opportunity_signals ADD COLUMN IF NOT EXISTS match_reason TEXT;
ALTER TABLE opportunity_signals ADD COLUMN IF NOT EXISTS admin_override_by TEXT;
ALTER TABLE opportunity_signals ADD COLUMN IF NOT EXISTS admin_override_at TIMESTAMPTZ;
ALTER TABLE opportunity_signals DROP CONSTRAINT IF EXISTS opportunity_signals_status_check;
ALTER TABLE opportunity_signals ADD CONSTRAINT opportunity_signals_status_check
    CHECK (status IN ('ACTIVE', 'REJECTED'));
ALTER TABLE opportunity_signals DROP CONSTRAINT IF EXISTS opportunity_signals_created_by_check;
ALTER TABLE opportunity_signals ADD CONSTRAINT opportunity_signals_created_by_check
    CHECK (created_by IN ('SYSTEM', 'ADMIN'));
ALTER TABLE opportunity_signals DROP CONSTRAINT IF EXISTS opportunity_signals_match_outcome_check;
ALTER TABLE opportunity_signals ADD CONSTRAINT opportunity_signals_match_outcome_check
    CHECK (match_outcome IN ('EXACT', 'STRONG', 'PROBABLE', 'UNCERTAIN', 'NO_MATCH'));

CREATE INDEX IF NOT EXISTS opportunity_signals_active_signal_idx
    ON opportunity_signals (raw_signal_id) WHERE status = 'ACTIVE';
CREATE INDEX IF NOT EXISTS opportunities_vertical_idx ON opportunities (vertical);

CREATE TABLE IF NOT EXISTS opportunity_match_reviews (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    raw_signal_id UUID NOT NULL REFERENCES raw_signals(id) ON DELETE CASCADE,
    opportunity_id UUID NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
    outcome TEXT NOT NULL CHECK (outcome IN ('UNCERTAIN', 'PROBABLE')),
    confidence NUMERIC(5, 4) CHECK (confidence >= 0 AND confidence <= 1),
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'LINKED', 'REJECTED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_by TEXT,
    reviewed_at TIMESTAMPTZ,
    UNIQUE (raw_signal_id, opportunity_id)
);
CREATE INDEX IF NOT EXISTS opportunity_match_reviews_status_idx
    ON opportunity_match_reviews (status, created_at DESC);

CREATE TABLE IF NOT EXISTS opportunity_signal_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    opportunity_id UUID NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
    raw_signal_id UUID NOT NULL REFERENCES raw_signals(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS opportunity_signal_history_signal_idx
    ON opportunity_signal_history (raw_signal_id, created_at DESC);
