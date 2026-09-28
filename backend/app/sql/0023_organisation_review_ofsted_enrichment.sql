-- Deduplicate bounded administrator requests for missing URN-specific evidence.
ALTER TABLE organisation_match_reviews
    ADD COLUMN IF NOT EXISTS ofsted_enrichment_requested_urn TEXT,
    ADD COLUMN IF NOT EXISTS ofsted_enrichment_requested_at TIMESTAMPTZ;
