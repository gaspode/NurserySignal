-- Customer access is explicitly vertical-scoped.  Existing CareProspect
-- accounts retain their current product access; Nursery access is granted
-- deliberately by an administrator.
CREATE TABLE IF NOT EXISTS customer_account_verticals (
    account_id UUID NOT NULL REFERENCES customer_accounts(id) ON DELETE CASCADE,
    vertical TEXT NOT NULL CHECK (vertical IN ('CHILDRENS_HOME', 'NURSERY')),
    granted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    granted_by TEXT NOT NULL DEFAULT 'migration:0034',
    PRIMARY KEY (account_id, vertical)
);

ALTER TABLE opportunities DROP CONSTRAINT IF EXISTS opportunities_customer_lifecycle_stage_check;
ALTER TABLE opportunities ADD CONSTRAINT opportunities_customer_lifecycle_stage_check
    CHECK (customer_lifecycle_stage IS NULL OR customer_lifecycle_stage IN (
        'PLANNING_PENDING', 'PLANNING_APPROVED', 'DELIVERY_SIGNAL_DETECTED',
        'REGISTRATION_DETECTED', 'REGISTERED', 'APPEAL_PENDING', 'STOPPED',
        'NEEDS_REVIEW', 'RECRUITMENT_ACTIVITY', 'OTHER_CHANGE', 'UNDER_REVIEW'
    ));

INSERT INTO customer_account_verticals (account_id, vertical, granted_by)
SELECT id, 'CHILDRENS_HOME', 'migration:0034'
FROM customer_accounts
ON CONFLICT (account_id, vertical) DO NOTHING;

CREATE INDEX IF NOT EXISTS opportunities_customer_published_vertical_idx
    ON opportunities (vertical, latest_update_at DESC)
    WHERE publication_status = 'PUBLISHED';

CREATE TABLE IF NOT EXISTS nursery_customer_publication_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    policy_version TEXT NOT NULL,
    actor TEXT NOT NULL,
    requested_limit INTEGER NOT NULL CHECK (requested_limit BETWEEN 1 AND 25),
    selected_count INTEGER NOT NULL DEFAULT 0,
    published_count INTEGER NOT NULL DEFAULT 0,
    skipped_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS nursery_customer_publication_run_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL REFERENCES nursery_customer_publication_runs(id) ON DELETE CASCADE,
    opportunity_id UUID NOT NULL REFERENCES opportunities(id),
    status TEXT NOT NULL CHECK (status IN ('PUBLISHED', 'SKIPPED', 'FAILED')),
    reason TEXT NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, opportunity_id)
);
