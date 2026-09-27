-- Evaluation-only historical research. These records never participate in
-- production ingestion, opportunities, lifecycle or review state.
CREATE TABLE IF NOT EXISTS benchmark_research_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    benchmark_version TEXT NOT NULL,
    corpus_version TEXT NOT NULL,
    vertical TEXT NOT NULL CHECK (vertical IN ('NURSERY', 'CHILDRENS_HOME', 'DENTAL')),
    status TEXT NOT NULL CHECK (status IN ('IMPORTING', 'COMPLETE', 'FAILED')),
    manifest_bucket TEXT NOT NULL,
    manifest_key TEXT NOT NULL,
    manifest_sha256 CHAR(64) NOT NULL,
    parameters JSONB NOT NULL DEFAULT '{}'::jsonb,
    summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    failure_category TEXT,
    failure_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (benchmark_version, corpus_version, manifest_sha256)
);

CREATE TABLE IF NOT EXISTS historical_research_records (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    corpus_version TEXT NOT NULL,
    vertical TEXT NOT NULL CHECK (vertical IN ('NURSERY', 'CHILDRENS_HOME', 'DENTAL')),
    source_type TEXT NOT NULL CHECK (source_type IN ('planning', 'recruitment')),
    provider TEXT NOT NULL,
    external_id TEXT NOT NULL,
    source_url TEXT NOT NULL,
    title TEXT NOT NULL,
    raw_text TEXT NOT NULL,
    organisation_hint TEXT,
    location_hint TEXT,
    source_event_at TIMESTAMPTZ,
    available_at TIMESTAMPTZ NOT NULL,
    retrieved_at TIMESTAMPTZ NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
    content_sha256 CHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (corpus_version, provider, source_type, external_id),
    UNIQUE (corpus_version, content_sha256)
);
CREATE INDEX IF NOT EXISTS historical_research_records_replay_idx
    ON historical_research_records (vertical, available_at, source_type);

CREATE TABLE IF NOT EXISTS benchmark_case_research (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    research_run_id UUID NOT NULL REFERENCES benchmark_research_runs(id) ON DELETE CASCADE,
    benchmark_case_id UUID NOT NULL REFERENCES benchmark_cases(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('COMPLETE', 'PARTIAL', 'FAILED')),
    sources_searched JSONB NOT NULL DEFAULT '[]'::jsonb,
    search_strategy JSONB NOT NULL DEFAULT '{}'::jsonb,
    records_found INTEGER NOT NULL DEFAULT 0,
    records_accepted INTEGER NOT NULL DEFAULT 0,
    records_rejected INTEGER NOT NULL DEFAULT 0,
    not_found_reason TEXT CHECK (not_found_reason IS NULL OR not_found_reason IN (
        'NOT_FOUND', 'SOURCE_NOT_HISTORICALLY_SEARCHABLE',
        'FOUND_BUT_NOT_RELIABLY_DATED', 'FOUND_BUT_CASE_LINK_UNCERTAIN'
    )),
    notes TEXT,
    researched_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (research_run_id, benchmark_case_id)
);

CREATE TABLE IF NOT EXISTS benchmark_case_research_evidence (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    research_run_id UUID NOT NULL REFERENCES benchmark_research_runs(id) ON DELETE CASCADE,
    benchmark_case_id UUID NOT NULL REFERENCES benchmark_cases(id) ON DELETE CASCADE,
    historical_record_id UUID NOT NULL REFERENCES historical_research_records(id) ON DELETE RESTRICT,
    case_link_confidence TEXT NOT NULL CHECK (case_link_confidence IN (
        'VERIFIED_CASE_LINK', 'STRONG_CASE_LINK', 'POSSIBLE_CASE_LINK', 'REJECTED_CASE_LINK'
    )),
    eligibility TEXT NOT NULL CHECK (eligibility IN (
        'ELIGIBLE', 'EXCLUDED_AFTER_REGISTRATION',
        'EXCLUDED_NO_RELIABLE_PUBLICATION_DATE', 'EXCLUDED_WEAK_CASE_LINK',
        'EXCLUDED_UNTRUSTWORTHY_SOURCE', 'EXCLUDED_FUTURE_INFORMATION'
    )),
    link_reason TEXT NOT NULL,
    rejection_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (research_run_id, benchmark_case_id, historical_record_id)
);
CREATE INDEX IF NOT EXISTS benchmark_case_research_evidence_eligible_idx
    ON benchmark_case_research_evidence (benchmark_case_id, historical_record_id)
    WHERE eligibility = 'ELIGIBLE';

ALTER TABLE backtest_runs ADD COLUMN IF NOT EXISTS corpus_version TEXT;
