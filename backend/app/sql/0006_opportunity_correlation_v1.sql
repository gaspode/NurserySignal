ALTER TABLE opportunities DROP CONSTRAINT IF EXISTS opportunities_lifecycle_stage_check;
ALTER TABLE opportunities ADD CONSTRAINT opportunities_lifecycle_stage_check
    CHECK (lifecycle_stage IN ('DISCOVERED', 'PLANNING', 'STAFFING', 'APPROVED', 'FIT_OUT',
        'RECRUITING', 'REGISTRATION', 'OPENING_SOON', 'OPEN'));

ALTER TABLE opportunities ADD COLUMN IF NOT EXISTS confidence_breakdown JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE opportunities ADD COLUMN IF NOT EXISTS stage_reason TEXT;

CREATE INDEX IF NOT EXISTS opportunity_signals_signal_idx ON opportunity_signals (raw_signal_id);
CREATE INDEX IF NOT EXISTS raw_signals_source_type_idx ON raw_signals (source_type);
