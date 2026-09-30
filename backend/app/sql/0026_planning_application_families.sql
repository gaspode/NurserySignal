-- Authority-scoped Planning application families and bounded origin recovery.
ALTER TABLE raw_signals ADD COLUMN IF NOT EXISTS planning_authority_normalized TEXT;
ALTER TABLE raw_signals ADD COLUMN IF NOT EXISTS planning_reference_normalized TEXT;

CREATE INDEX IF NOT EXISTS raw_signals_planning_reference_idx
    ON raw_signals (vertical, planning_authority_normalized, planning_reference_normalized)
    WHERE source_type = 'planning' AND planning_reference_normalized IS NOT NULL;

CREATE TABLE IF NOT EXISTS planning_application_families (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    vertical TEXT NOT NULL CHECK (vertical IN ('NURSERY', 'CHILDRENS_HOME', 'DENTAL')),
    planning_authority TEXT NOT NULL,
    normalized_authority TEXT NOT NULL,
    raw_reference TEXT NOT NULL,
    normalized_reference TEXT NOT NULL,
    primary_signal_id UUID REFERENCES raw_signals(id) ON DELETE SET NULL,
    origin_status TEXT NOT NULL DEFAULT 'MISSING'
        CHECK (origin_status IN ('MISSING', 'RESOLVED', 'NEGATIVE', 'AMBIGUOUS')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (vertical, normalized_authority, normalized_reference)
);

CREATE TABLE IF NOT EXISTS planning_signal_family_relationships (
    family_id UUID NOT NULL REFERENCES planning_application_families(id) ON DELETE CASCADE,
    raw_signal_id UUID NOT NULL REFERENCES raw_signals(id) ON DELETE CASCADE,
    relationship_type TEXT NOT NULL
        CHECK (relationship_type IN ('PRIMARY_APPLICATION', 'REFERENCES_APPLICATION',
                                     'REVISION', 'RESUBMISSION', 'APPEAL', 'OTHER_RELATED')),
    provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
    deterministic BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (family_id, raw_signal_id, relationship_type)
);

CREATE INDEX IF NOT EXISTS planning_signal_family_signal_idx
    ON planning_signal_family_relationships (raw_signal_id);

CREATE TABLE IF NOT EXISTS planning_origin_recovery_attempts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    family_id UUID NOT NULL REFERENCES planning_application_families(id) ON DELETE CASCADE,
    triggering_signal_id UUID REFERENCES raw_signals(id) ON DELETE SET NULL,
    provider TEXT NOT NULL DEFAULT 'Plota',
    status TEXT NOT NULL CHECK (status IN ('QUEUED', 'RUNNING', 'FOUND', 'NOT_FOUND',
                                           'AMBIGUOUS', 'PROVIDER_ERROR')),
    attempted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    retry_after TIMESTAMPTZ,
    recovered_signal_id UUID REFERENCES raw_signals(id) ON DELETE SET NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS planning_origin_recovery_family_idx
    ON planning_origin_recovery_attempts (family_id, attempted_at DESC);
