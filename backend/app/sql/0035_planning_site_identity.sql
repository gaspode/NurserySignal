ALTER TABLE opportunities
    ADD COLUMN IF NOT EXISTS site_identity_provenance JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE INDEX IF NOT EXISTS opportunities_site_identity_postcode_idx
    ON opportunities (vertical, postcode)
    WHERE review_status NOT IN ('MERGED', 'REJECTED');
