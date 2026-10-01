-- Phase D2: bounded automatic CareProspect withdrawal runtime and audit history.
ALTER TABLE opportunities
    ADD COLUMN IF NOT EXISTS customer_withdrawn_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS withdrawal_automation_provenance JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE TABLE IF NOT EXISTS care_withdrawal_automation_state (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    execution_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    recurring_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    emergency_reason TEXT,
    max_withdrawals_per_execution INTEGER NOT NULL DEFAULT 10
        CHECK (max_withdrawals_per_execution BETWEEN 1 AND 50),
    policy_version TEXT NOT NULL DEFAULT 'care-withdrawal-v1',
    last_execution_at TIMESTAMPTZ,
    last_selected INTEGER NOT NULL DEFAULT 0,
    last_withdrawn INTEGER NOT NULL DEFAULT 0,
    last_skipped INTEGER NOT NULL DEFAULT 0,
    last_failed INTEGER NOT NULL DEFAULT 0,
    updated_by TEXT NOT NULL DEFAULT 'SYSTEM',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO care_withdrawal_automation_state (singleton)
VALUES (TRUE) ON CONFLICT (singleton) DO NOTHING;

CREATE TABLE IF NOT EXISTS care_withdrawal_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    policy_version TEXT NOT NULL,
    actor TEXT NOT NULL,
    trigger_source TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('RUNNING', 'COMPLETED', 'PARTIAL', 'FAILED')),
    requested_limit INTEGER NOT NULL CHECK (requested_limit BETWEEN 1 AND 50),
    selected_count INTEGER NOT NULL DEFAULT 0,
    withdrawn_count INTEGER NOT NULL DEFAULT 0,
    skipped_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS care_withdrawal_runs_created_idx
    ON care_withdrawal_runs (created_at DESC);

CREATE TABLE IF NOT EXISTS care_withdrawal_run_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL REFERENCES care_withdrawal_runs(id) ON DELETE CASCADE,
    opportunity_id UUID NOT NULL REFERENCES opportunities(id),
    status TEXT NOT NULL CHECK (status IN ('WITHDRAWN', 'SKIPPED', 'FAILED')),
    policy_outcome TEXT,
    reason TEXT,
    lifecycle TEXT,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, opportunity_id)
);

CREATE INDEX IF NOT EXISTS care_withdrawal_run_items_opportunity_idx
    ON care_withdrawal_run_items (opportunity_id, created_at DESC);
