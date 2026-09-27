-- Isolated, versioned historical evaluation state. Benchmark truth never
-- participates in production opportunity matching.
CREATE TABLE IF NOT EXISTS benchmark_cases (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    benchmark_case_id TEXT NOT NULL,
    benchmark_version TEXT NOT NULL,
    vertical TEXT NOT NULL CHECK (vertical IN ('NURSERY', 'CHILDRENS_HOME', 'DENTAL')),
    known_operator TEXT,
    known_site TEXT,
    known_location TEXT,
    known_postcode TEXT,
    known_company_number TEXT,
    known_regulatory_id TEXT,
    outcome_type TEXT NOT NULL CHECK (outcome_type IN (
        'OPENED', 'REGISTERED', 'EXPANDED', 'RELOCATED', 'DID_NOT_OPEN', 'ABANDONED'
    )),
    outcome_date DATE NOT NULL,
    provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
    label_confidence TEXT NOT NULL CHECK (label_confidence IN ('HIGH', 'MEDIUM', 'LOW')),
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (benchmark_version, benchmark_case_id)
);
CREATE INDEX IF NOT EXISTS benchmark_cases_version_vertical_idx
    ON benchmark_cases (benchmark_version, vertical, outcome_date);

CREATE TABLE IF NOT EXISTS backtest_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_fingerprint CHAR(64) NOT NULL UNIQUE,
    benchmark_version TEXT NOT NULL,
    engine_version TEXT NOT NULL,
    vertical TEXT NOT NULL CHECK (vertical IN ('NURSERY', 'CHILDRENS_HOME', 'DENTAL')),
    as_of TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('RUNNING', 'SUCCESS', 'FAILED')),
    parameters JSONB NOT NULL DEFAULT '{}'::jsonb,
    metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_contribution JSONB NOT NULL DEFAULT '{}'::jsonb,
    failure_category TEXT,
    failure_message TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS backtest_runs_version_idx
    ON backtest_runs (benchmark_version, vertical, started_at DESC);

CREATE TABLE IF NOT EXISTS backtest_case_results (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL REFERENCES backtest_runs(id) ON DELETE CASCADE,
    benchmark_case_id UUID NOT NULL REFERENCES benchmark_cases(id) ON DELETE RESTRICT,
    usable BOOLEAN NOT NULL,
    exclusion_reason TEXT,
    detected BOOLEAN NOT NULL DEFAULT FALSE,
    opportunity_created BOOLEAN NOT NULL DEFAULT FALSE,
    first_discovered_at TIMESTAMPTZ,
    first_opportunity_at TIMESTAMPTZ,
    first_source TEXT,
    lead_time_days INTEGER,
    organisation_resolution TEXT,
    organisation_resolved_at TIMESTAMPTZ,
    companies_house_improved_identity BOOLEAN NOT NULL DEFAULT FALSE,
    site_resolution TEXT,
    duplicate_opportunities INTEGER NOT NULL DEFAULT 0,
    incorrect_merges INTEGER NOT NULL DEFAULT 0,
    review_items INTEGER NOT NULL DEFAULT 0,
    source_dates JSONB NOT NULL DEFAULT '{}'::jsonb,
    timeline JSONB NOT NULL DEFAULT '[]'::jsonb,
    generated_opportunities JSONB NOT NULL DEFAULT '[]'::jsonb,
    confidence_metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, benchmark_case_id)
);

-- Reusable historical labels. These are evaluation output only and are never
-- consumed by live matching or classification.
CREATE OR REPLACE VIEW backtest_labelled_decisions AS
SELECT
    mr.id AS decision_id,
    mr.vertical,
    'OPPORTUNITY_MATCH'::text AS decision_type,
    mr.raw_signal_id::text AS left_record_id,
    mr.opportunity_id::text AS right_record_id,
    CASE WHEN mr.status = 'LINKED' THEN 'MATCH' ELSE 'NOT_SAME' END AS decision,
    mr.reviewed_at AS decided_at,
    mr.reviewed_by AS actor,
    jsonb_build_object(
        'outcome', mr.outcome,
        'confidence', mr.confidence,
        'reason', mr.reason
    ) AS features
FROM opportunity_match_reviews mr
WHERE mr.status IN ('LINKED', 'REJECTED') AND mr.reviewed_at IS NOT NULL
UNION ALL
SELECT
    omr.id,
    COALESCE(activity.vertical, 'ALL') AS vertical,
    'ORGANISATION_RESOLUTION'::text,
    omr.operator_id::text,
    omr.provider,
    CASE WHEN omr.status = 'CONFIRMED' THEN 'MATCH' ELSE 'NOT_SAME' END,
    omr.reviewed_at,
    omr.reviewed_by,
    jsonb_build_object(
        'query_name', omr.query_name,
        'candidate_count', jsonb_array_length(omr.candidates),
        'reason', omr.reason
    )
FROM organisation_match_reviews omr
LEFT JOIN LATERAL (
    SELECT CASE WHEN count(DISTINCT o.vertical) = 1 THEN min(o.vertical) ELSE 'ALL' END AS vertical
    FROM opportunities o
    WHERE o.operator_id = omr.operator_id
) activity ON TRUE
WHERE omr.status IN ('CONFIRMED', 'REJECTED') AND omr.reviewed_at IS NOT NULL;

-- Seed only authoritative, already-preserved 2025–2026 CareSignal outcomes.
-- Registration is benchmark truth and the replay engine explicitly excludes
-- Ofsted from all pre-registration input.
INSERT INTO benchmark_cases (
    benchmark_case_id, benchmark_version, vertical, known_operator,
    known_location, known_regulatory_id, outcome_type, outcome_date,
    provenance, label_confidence, notes
)
SELECT
    'ofsted:' || (rs.metadata->>'ofsted_urn'),
    'care-ofsted-v1',
    'CHILDRENS_HOME',
    rs.organisation_hint,
    rs.metadata->>'local_authority',
    rs.metadata->>'ofsted_urn',
    'REGISTERED',
    (rs.metadata->>'registration_date')::date,
    jsonb_build_object(
        'source', 'OFSTED_REGISTER',
        'raw_signal_id', rs.id::text,
        'external_id', rs.external_id,
        'source_url', rs.source_url,
        'truth_role', 'OUTCOME_ONLY'
    ),
    'HIGH',
    'Official Ofsted registration date; Ofsted is excluded from pre-registration replay.'
FROM (
    SELECT * FROM raw_signals
    WHERE vertical = 'CHILDRENS_HOME'
      AND source_type = 'ofsted'
      AND metadata->>'ofsted_urn' IS NOT NULL
      AND metadata->>'registration_date' ~ '^20(25|26)-[0-9]{2}-[0-9]{2}$'
      AND COALESCE(organisation_hint, '') <> ''
    ORDER BY (metadata->>'registration_date')::date DESC, id
    LIMIT 30
) rs
ON CONFLICT (benchmark_version, benchmark_case_id) DO NOTHING;
