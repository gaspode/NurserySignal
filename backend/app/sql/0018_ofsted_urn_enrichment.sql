-- Keep URN-specific public Ofsted metadata separate from the annual-register
-- signal and explicitly distinguish provider identity from home/site identity.
CREATE TABLE IF NOT EXISTS ofsted_urn_enrichments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    raw_signal_id UUID NOT NULL REFERENCES raw_signals(id) ON DELETE CASCADE,
    operator_id UUID REFERENCES operators(id) ON DELETE SET NULL,
    urn TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('SUCCEEDED', 'PARTIAL', 'NO_REPORT', 'FAILED')),
    provider_page_url TEXT NOT NULL,
    provision_type TEXT,
    registration_date DATE,
    local_authority TEXT,
    registered_provider_name TEXT,
    provider_registered_address TEXT,
    provider_registered_locality TEXT,
    provider_registered_region TEXT,
    provider_registered_postcode TEXT,
    latest_report_type TEXT,
    latest_report_date DATE,
    latest_report_publication_date DATE,
    latest_report_url TEXT,
    report_count INTEGER NOT NULL DEFAULT 0,
    report_content_sha256 CHAR(64),
    parser_version TEXT NOT NULL,
    failure_category TEXT,
    evidence_bucket TEXT NOT NULL,
    evidence_key TEXT NOT NULL,
    content_sha256 CHAR(64) NOT NULL,
    retrieved_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (urn, content_sha256)
);

CREATE INDEX IF NOT EXISTS ofsted_urn_enrichments_signal_idx
    ON ofsted_urn_enrichments (raw_signal_id, retrieved_at DESC);
CREATE INDEX IF NOT EXISTS ofsted_urn_enrichments_operator_idx
    ON ofsted_urn_enrichments (operator_id, retrieved_at DESC)
    WHERE operator_id IS NOT NULL;
