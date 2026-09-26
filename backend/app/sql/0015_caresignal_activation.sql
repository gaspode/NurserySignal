-- Activate CareSignal while keeping exact residential locations internal-only.
ALTER TABLE raw_signals
    ADD COLUMN IF NOT EXISTS location_sensitivity TEXT NOT NULL DEFAULT 'STANDARD';
ALTER TABLE raw_signals DROP CONSTRAINT IF EXISTS raw_signals_location_sensitivity_check;
ALTER TABLE raw_signals ADD CONSTRAINT raw_signals_location_sensitivity_check
    CHECK (location_sensitivity IN ('STANDARD', 'INTERNAL_EXACT'));

ALTER TABLE opportunities
    ADD COLUMN IF NOT EXISTS location_sensitivity TEXT NOT NULL DEFAULT 'STANDARD';
ALTER TABLE opportunities DROP CONSTRAINT IF EXISTS opportunities_location_sensitivity_check;
ALTER TABLE opportunities ADD CONSTRAINT opportunities_location_sensitivity_check
    CHECK (location_sensitivity IN ('STANDARD', 'INTERNAL_EXACT'));

UPDATE signal_sources
SET supported_verticals = '["NURSERY", "CHILDRENS_HOME"]'::jsonb,
    updated_at = now()
WHERE source_type IN ('planning', 'recruitment');
