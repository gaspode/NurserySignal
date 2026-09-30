-- Phase A: explicit customer lifecycle, immutable transition history and watched Planning state.
ALTER TABLE opportunities
    ADD COLUMN IF NOT EXISTS customer_lifecycle_stage TEXT
        CHECK (customer_lifecycle_stage IS NULL OR customer_lifecycle_stage IN (
            'PLANNING_PENDING', 'PLANNING_APPROVED', 'DELIVERY_SIGNAL_DETECTED',
            'REGISTRATION_DETECTED', 'REGISTERED', 'APPEAL_PENDING', 'STOPPED',
            'NEEDS_REVIEW'
        )),
    ADD COLUMN IF NOT EXISTS customer_lifecycle_reason TEXT,
    ADD COLUMN IF NOT EXISTS customer_lifecycle_policy_version TEXT,
    ADD COLUMN IF NOT EXISTS customer_lifecycle_evaluated_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS publication_automation_blocked BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS publication_automation_reason TEXT,
    ADD COLUMN IF NOT EXISTS publication_automation_provenance JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE INDEX IF NOT EXISTS opportunities_customer_lifecycle_idx
    ON opportunities (customer_lifecycle_stage)
    WHERE vertical = 'CHILDRENS_HOME';

CREATE TABLE IF NOT EXISTS opportunity_lifecycle_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    opportunity_id UUID NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
    old_lifecycle TEXT,
    new_lifecycle TEXT NOT NULL,
    reason TEXT NOT NULL,
    triggering_signal_ids UUID[] NOT NULL DEFAULT '{}',
    source_type TEXT,
    policy_version TEXT NOT NULL,
    actor_type TEXT NOT NULL CHECK (actor_type IN ('AUTOMATED', 'MANUAL', 'BOOTSTRAP')),
    actor TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS opportunity_lifecycle_history_opportunity_idx
    ON opportunity_lifecycle_history (opportunity_id, created_at DESC);

CREATE TABLE IF NOT EXISTS planning_lifecycle_watches (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    family_id UUID NOT NULL REFERENCES planning_application_families(id) ON DELETE CASCADE,
    opportunity_id UUID NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
    primary_signal_id UUID NOT NULL REFERENCES raw_signals(id) ON DELETE CASCADE,
    latest_outcome TEXT NOT NULL,
    last_checked_at TIMESTAMPTZ,
    next_eligible_refresh_at TIMESTAMPTZ NOT NULL,
    last_provider_result TEXT,
    status_changed_at TIMESTAMPTZ,
    consecutive_provider_errors INTEGER NOT NULL DEFAULT 0,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    policy_version TEXT NOT NULL DEFAULT 'care-opportunity-lifecycle-v1',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (family_id, opportunity_id)
);

CREATE INDEX IF NOT EXISTS planning_lifecycle_watches_due_idx
    ON planning_lifecycle_watches (next_eligible_refresh_at)
    WHERE enabled;
