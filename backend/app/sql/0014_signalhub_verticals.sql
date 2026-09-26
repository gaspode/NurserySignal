-- SignalHub vertical registry and isolation safeguards. Existing records are
-- NurserySignal data and are migrated conservatively to NURSERY.
ALTER TABLE raw_signals ADD COLUMN IF NOT EXISTS vertical TEXT NOT NULL DEFAULT 'NURSERY';
ALTER TABLE raw_signals DROP CONSTRAINT IF EXISTS raw_signals_vertical_check;
ALTER TABLE raw_signals ADD CONSTRAINT raw_signals_vertical_check
    CHECK (vertical IN ('NURSERY', 'CHILDRENS_HOME', 'DENTAL'));
ALTER TABLE raw_signals DROP CONSTRAINT IF EXISTS raw_signals_source_type_external_id_key;
CREATE UNIQUE INDEX IF NOT EXISTS raw_signals_vertical_source_external_uq
    ON raw_signals (vertical, source_type, external_id);

ALTER TABLE source_documents ADD COLUMN IF NOT EXISTS vertical TEXT NOT NULL DEFAULT 'NURSERY';
UPDATE source_documents d SET vertical = s.vertical FROM raw_signals s WHERE s.id = d.raw_signal_id;
ALTER TABLE source_documents DROP CONSTRAINT IF EXISTS source_documents_vertical_check;
ALTER TABLE source_documents ADD CONSTRAINT source_documents_vertical_check
    CHECK (vertical IN ('NURSERY', 'CHILDRENS_HOME', 'DENTAL'));

ALTER TABLE raw_signal_revisions ADD COLUMN IF NOT EXISTS vertical TEXT NOT NULL DEFAULT 'NURSERY';
UPDATE raw_signal_revisions r SET vertical = s.vertical FROM raw_signals s WHERE s.id = r.raw_signal_id;
ALTER TABLE raw_signal_revisions DROP CONSTRAINT IF EXISTS raw_signal_revisions_vertical_check;
ALTER TABLE raw_signal_revisions ADD CONSTRAINT raw_signal_revisions_vertical_check
    CHECK (vertical IN ('NURSERY', 'CHILDRENS_HOME', 'DENTAL'));

ALTER TABLE signal_enrichments ADD COLUMN IF NOT EXISTS vertical TEXT NOT NULL DEFAULT 'NURSERY';
UPDATE signal_enrichments e SET vertical = s.vertical FROM raw_signals s WHERE s.id = e.raw_signal_id;
ALTER TABLE signal_enrichments DROP CONSTRAINT IF EXISTS signal_enrichments_vertical_check;
ALTER TABLE signal_enrichments ADD CONSTRAINT signal_enrichments_vertical_check
    CHECK (vertical IN ('NURSERY', 'CHILDRENS_HOME', 'DENTAL'));

ALTER TABLE opportunities DROP CONSTRAINT IF EXISTS opportunities_vertical_check;
ALTER TABLE opportunities ADD CONSTRAINT opportunities_vertical_check
    CHECK (vertical IN ('NURSERY', 'CHILDRENS_HOME', 'DENTAL'));

ALTER TABLE opportunity_signals ADD COLUMN IF NOT EXISTS vertical TEXT NOT NULL DEFAULT 'NURSERY';
UPDATE opportunity_signals os SET vertical = o.vertical
FROM opportunities o WHERE o.id = os.opportunity_id;
ALTER TABLE opportunity_signals DROP CONSTRAINT IF EXISTS opportunity_signals_vertical_check;
ALTER TABLE opportunity_signals ADD CONSTRAINT opportunity_signals_vertical_check
    CHECK (vertical IN ('NURSERY', 'CHILDRENS_HOME', 'DENTAL'));

ALTER TABLE opportunity_match_reviews ADD COLUMN IF NOT EXISTS vertical TEXT NOT NULL DEFAULT 'NURSERY';
UPDATE opportunity_match_reviews mr SET vertical = s.vertical
FROM raw_signals s WHERE s.id = mr.raw_signal_id;
ALTER TABLE opportunity_match_reviews DROP CONSTRAINT IF EXISTS opportunity_match_reviews_vertical_check;
ALTER TABLE opportunity_match_reviews ADD CONSTRAINT opportunity_match_reviews_vertical_check
    CHECK (vertical IN ('NURSERY', 'CHILDRENS_HOME', 'DENTAL'));

ALTER TABLE signal_ai_reviews ADD COLUMN IF NOT EXISTS vertical TEXT NOT NULL DEFAULT 'NURSERY';
UPDATE signal_ai_reviews ai SET vertical = s.vertical
FROM raw_signals s WHERE s.id = ai.raw_signal_id;
ALTER TABLE signal_ai_reviews DROP CONSTRAINT IF EXISTS signal_ai_reviews_vertical_check;
ALTER TABLE signal_ai_reviews ADD CONSTRAINT signal_ai_reviews_vertical_check
    CHECK (vertical IN ('NURSERY', 'CHILDRENS_HOME', 'DENTAL'));

ALTER TABLE admin_audit_events ADD COLUMN IF NOT EXISTS vertical TEXT;
ALTER TABLE signal_sources ADD COLUMN IF NOT EXISTS supported_verticals JSONB NOT NULL DEFAULT '["NURSERY"]'::jsonb;

CREATE INDEX IF NOT EXISTS raw_signals_vertical_idx ON raw_signals (vertical, discovered_at DESC);
CREATE INDEX IF NOT EXISTS opportunities_vertical_current_idx
    ON opportunities (vertical, review_status);

CREATE OR REPLACE FUNCTION signalhub_check_opportunity_signal_vertical()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE signal_vertical TEXT; opportunity_vertical TEXT;
BEGIN
    SELECT vertical INTO signal_vertical FROM raw_signals WHERE id = NEW.raw_signal_id;
    SELECT vertical INTO opportunity_vertical FROM opportunities WHERE id = NEW.opportunity_id;
    IF signal_vertical IS NULL OR opportunity_vertical IS NULL OR signal_vertical <> opportunity_vertical THEN
        RAISE EXCEPTION 'signal and opportunity verticals must match';
    END IF;
    NEW.vertical := signal_vertical;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS opportunity_signals_vertical_guard ON opportunity_signals;
CREATE TRIGGER opportunity_signals_vertical_guard
BEFORE INSERT OR UPDATE ON opportunity_signals
FOR EACH ROW EXECUTE FUNCTION signalhub_check_opportunity_signal_vertical();

CREATE OR REPLACE FUNCTION signalhub_check_match_review_vertical()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE signal_vertical TEXT; opportunity_vertical TEXT;
BEGIN
    SELECT vertical INTO signal_vertical FROM raw_signals WHERE id = NEW.raw_signal_id;
    SELECT vertical INTO opportunity_vertical FROM opportunities WHERE id = NEW.opportunity_id;
    IF signal_vertical IS NULL OR opportunity_vertical IS NULL OR signal_vertical <> opportunity_vertical THEN
        RAISE EXCEPTION 'match review signal and opportunity verticals must match';
    END IF;
    NEW.vertical := signal_vertical;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS opportunity_match_reviews_vertical_guard ON opportunity_match_reviews;
CREATE TRIGGER opportunity_match_reviews_vertical_guard
BEFORE INSERT OR UPDATE ON opportunity_match_reviews
FOR EACH ROW EXECUTE FUNCTION signalhub_check_match_review_vertical();
