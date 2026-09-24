CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS operators (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    legal_name TEXT,
    companies_house_number TEXT,
    website_url TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS operators_companies_house_number_uq
    ON operators (companies_house_number)
    WHERE companies_house_number IS NOT NULL;

CREATE TABLE IF NOT EXISTS nurseries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    operator_id UUID REFERENCES operators(id) ON DELETE SET NULL,
    name TEXT NOT NULL,
    address_line_1 TEXT,
    address_line_2 TEXT,
    town TEXT,
    county TEXT,
    postcode TEXT,
    latitude NUMERIC(9, 6),
    longitude NUMERIC(9, 6),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS opportunities (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    nursery_id UUID REFERENCES nurseries(id) ON DELETE SET NULL,
    operator_id UUID REFERENCES operators(id) ON DELETE SET NULL,
    name TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK (event_type IN ('opening', 'expansion', 'other')),
    lifecycle_stage TEXT NOT NULL DEFAULT 'DISCOVERED'
        CHECK (lifecycle_stage IN ('DISCOVERED', 'PLANNING', 'APPROVED', 'FIT_OUT',
        'RECRUITING', 'REGISTRATION', 'OPENING_SOON', 'OPEN')),
    expected_opening_date DATE,
    capacity INTEGER CHECK (capacity IS NULL OR capacity >= 0),
    confidence NUMERIC(5, 4) CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    review_status TEXT NOT NULL DEFAULT 'UNREVIEWED'
        CHECK (review_status IN ('UNREVIEWED', 'IN_REVIEW', 'APPROVED', 'REJECTED', 'MERGED')),
    publication_status TEXT NOT NULL DEFAULT 'DRAFT'
        CHECK (publication_status IN ('DRAFT', 'PUBLISHED', 'WITHDRAWN')),
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    latest_update_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS signal_sources (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_type TEXT NOT NULL,
    name TEXT NOT NULL,
    base_url TEXT,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_type, name)
);

CREATE TABLE IF NOT EXISTS raw_signals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id UUID REFERENCES signal_sources(id) ON DELETE SET NULL,
    source_type TEXT NOT NULL,
    source_url TEXT NOT NULL,
    external_id TEXT,
    discovered_at TIMESTAMPTZ NOT NULL,
    title TEXT NOT NULL,
    raw_text TEXT NOT NULL,
    location_hint TEXT,
    organisation_hint TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    content_sha256 CHAR(64),
    persisted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_type, external_id)
);

CREATE INDEX IF NOT EXISTS raw_signals_discovered_at_idx ON raw_signals (discovered_at DESC);

CREATE TABLE IF NOT EXISTS source_documents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    raw_signal_id UUID NOT NULL REFERENCES raw_signals(id) ON DELETE CASCADE,
    s3_bucket TEXT NOT NULL,
    s3_key TEXT NOT NULL,
    sha256 CHAR(64),
    mime_type TEXT,
    captured_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (s3_bucket, s3_key)
);

CREATE TABLE IF NOT EXISTS opportunity_signals (
    opportunity_id UUID NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
    raw_signal_id UUID NOT NULL REFERENCES raw_signals(id) ON DELETE CASCADE,
    relationship_type TEXT NOT NULL DEFAULT 'SUPPORTS',
    extracted_facts JSONB NOT NULL DEFAULT '{}'::jsonb,
    provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (opportunity_id, raw_signal_id)
);

CREATE INDEX IF NOT EXISTS opportunities_stage_idx ON opportunities (lifecycle_stage);
CREATE INDEX IF NOT EXISTS opportunities_review_status_idx ON opportunities (review_status);

