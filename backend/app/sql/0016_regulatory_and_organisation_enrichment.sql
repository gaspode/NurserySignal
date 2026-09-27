-- Ofsted regulatory evidence remains vertical-specific. Companies House
-- enrichment belongs to the shared organisation (operators) layer.
ALTER TABLE operators ADD COLUMN IF NOT EXISTS company_status TEXT;
ALTER TABLE operators ADD COLUMN IF NOT EXISTS incorporation_date DATE;
ALTER TABLE operators ADD COLUMN IF NOT EXISTS company_type TEXT;
ALTER TABLE operators ADD COLUMN IF NOT EXISTS registered_office JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE operators ADD COLUMN IF NOT EXISTS sic_codes JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE operators ADD COLUMN IF NOT EXISTS companies_house_url TEXT;
ALTER TABLE operators ADD COLUMN IF NOT EXISTS companies_house_refreshed_at TIMESTAMPTZ;
ALTER TABLE operators ADD COLUMN IF NOT EXISTS resolution_outcome TEXT;
ALTER TABLE operators ADD COLUMN IF NOT EXISTS resolution_confidence NUMERIC(5, 4);
ALTER TABLE operators ADD COLUMN IF NOT EXISTS enrichment_provenance JSONB NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE operators DROP CONSTRAINT IF EXISTS operators_resolution_outcome_check;
ALTER TABLE operators ADD CONSTRAINT operators_resolution_outcome_check
    CHECK (resolution_outcome IS NULL OR resolution_outcome IN
        ('EXACT', 'STRONG', 'PROBABLE', 'UNCERTAIN', 'NO_MATCH'));

CREATE TABLE IF NOT EXISTS organisation_aliases (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    operator_id UUID NOT NULL REFERENCES operators(id) ON DELETE CASCADE,
    alias TEXT NOT NULL,
    normalized_alias TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (operator_id, normalized_alias)
);

CREATE TABLE IF NOT EXISTS organisation_evidence (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    operator_id UUID REFERENCES operators(id) ON DELETE SET NULL,
    provider TEXT NOT NULL,
    external_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('MATCHED', 'AMBIGUOUS', 'NO_MATCH', 'FAILED')),
    resolution_outcome TEXT NOT NULL CHECK (resolution_outcome IN
        ('EXACT', 'STRONG', 'PROBABLE', 'UNCERTAIN', 'NO_MATCH')),
    resolution_confidence NUMERIC(5, 4),
    reason TEXT,
    evidence_bucket TEXT,
    evidence_key TEXT,
    content_sha256 CHAR(64),
    retrieved_at TIMESTAMPTZ NOT NULL,
    safe_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (provider, external_id, content_sha256)
);

CREATE INDEX IF NOT EXISTS organisation_evidence_operator_idx
    ON organisation_evidence (operator_id, retrieved_at DESC);

CREATE TABLE IF NOT EXISTS organisation_match_reviews (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    operator_id UUID NOT NULL REFERENCES operators(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    query_name TEXT NOT NULL,
    candidates JSONB NOT NULL DEFAULT '[]'::jsonb,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'CONFIRMED', 'REJECTED', 'SUPERSEDED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_by TEXT,
    reviewed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS organisation_match_reviews_pending_idx
    ON organisation_match_reviews (created_at DESC) WHERE status = 'PENDING';
CREATE UNIQUE INDEX IF NOT EXISTS organisation_match_reviews_one_pending_uq
    ON organisation_match_reviews (operator_id, provider) WHERE status = 'PENDING';

UPDATE signal_sources
SET supported_verticals = '[]'::jsonb,
    updated_at = now()
WHERE source_type = 'companies_house';

INSERT INTO signal_sources (source_type, name, base_url, is_active, supported_verticals)
VALUES
    ('ofsted', 'Ofsted children''s social care register',
     'https://www.gov.uk/government/publications/inspection-and-regulation-of-childrens-social-care-providers',
     true, '["CHILDRENS_HOME"]'::jsonb),
    ('companies_house', 'Companies House Public Data API',
     'https://api.company-information.service.gov.uk', true, '[]'::jsonb)
ON CONFLICT (source_type, name) DO UPDATE SET
    base_url = EXCLUDED.base_url,
    is_active = EXCLUDED.is_active,
    supported_verticals = EXCLUDED.supported_verticals,
    updated_at = now();
