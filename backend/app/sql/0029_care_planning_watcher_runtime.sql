-- Phase B3: durable, bounded Planning watcher runtime and append-only execution history.
ALTER TABLE planning_lifecycle_watches
    ALTER COLUMN family_id DROP NOT NULL,
    ADD COLUMN IF NOT EXISTS planning_authority TEXT,
    ADD COLUMN IF NOT EXISTS planning_reference TEXT,
    ADD COLUMN IF NOT EXISTS lifecycle_at_enrolment TEXT,
    ADD COLUMN IF NOT EXISTS cadence_days INTEGER CHECK (cadence_days IN (7, 14, 30)),
    ADD COLUMN IF NOT EXISTS activity_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS activity_source TEXT,
    ADD COLUMN IF NOT EXISTS latest_status_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS enrolment_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS disabled_reason TEXT,
    ADD COLUMN IF NOT EXISTS last_poll_started_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_poll_completed_at TIMESTAMPTZ;

ALTER TABLE planning_lifecycle_watches
    DROP CONSTRAINT IF EXISTS planning_lifecycle_watches_family_id_opportunity_id_key;

CREATE UNIQUE INDEX IF NOT EXISTS planning_lifecycle_watches_opportunity_signal_idx
    ON planning_lifecycle_watches (opportunity_id, primary_signal_id);

CREATE TABLE IF NOT EXISTS planning_lifecycle_watch_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    watch_id UUID NOT NULL REFERENCES planning_lifecycle_watches(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    old_enabled BOOLEAN,
    new_enabled BOOLEAN,
    old_cadence_days INTEGER,
    new_cadence_days INTEGER,
    reason TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    actor TEXT NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS planning_lifecycle_watch_history_watch_idx
    ON planning_lifecycle_watch_history (watch_id, created_at DESC);

CREATE TABLE IF NOT EXISTS planning_lifecycle_watch_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    watch_id UUID NOT NULL REFERENCES planning_lifecycle_watches(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN (
        'QUEUED', 'RUNNING', 'UNCHANGED', 'CHANGED', 'FAILED', 'RATE_LIMITED', 'QUEUE_FAILED'
    )),
    provider_requests INTEGER NOT NULL DEFAULT 0 CHECK (provider_requests >= 0),
    provider_result TEXT,
    error_category TEXT,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    queued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS planning_lifecycle_watch_runs_active_idx
    ON planning_lifecycle_watch_runs (watch_id)
    WHERE status IN ('QUEUED', 'RUNNING');
CREATE INDEX IF NOT EXISTS planning_lifecycle_watch_runs_created_idx
    ON planning_lifecycle_watch_runs (created_at DESC);

CREATE TABLE IF NOT EXISTS planning_lifecycle_watcher_state (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    execution_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    emergency_reason TEXT,
    max_polls_per_execution INTEGER NOT NULL DEFAULT 15 CHECK (max_polls_per_execution BETWEEN 1 AND 100),
    max_provider_requests_per_day INTEGER NOT NULL DEFAULT 50 CHECK (max_provider_requests_per_day BETWEEN 1 AND 1000),
    max_provider_requests_per_month INTEGER NOT NULL DEFAULT 2000 CHECK (max_provider_requests_per_month BETWEEN 1 AND 19000),
    policy_version TEXT NOT NULL DEFAULT 'care-planning-watcher-v2',
    updated_by TEXT NOT NULL DEFAULT 'SYSTEM',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO planning_lifecycle_watcher_state (singleton)
VALUES (TRUE) ON CONFLICT (singleton) DO NOTHING;
