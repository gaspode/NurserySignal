-- Customer-facing CareSignal pilot accounts. SignalHub records remain the
-- source of truth; only explicitly published opportunities are projected.
ALTER TABLE opportunities ADD COLUMN IF NOT EXISTS customer_title TEXT;
ALTER TABLE opportunities ADD COLUMN IF NOT EXISTS customer_summary TEXT;
ALTER TABLE opportunities ADD COLUMN IF NOT EXISTS customer_published_by TEXT;
ALTER TABLE opportunities ADD COLUMN IF NOT EXISTS customer_published_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS customer_accounts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PILOT'
        CHECK (status IN ('PILOT', 'ACTIVE', 'SUSPENDED')),
    plan TEXT NOT NULL DEFAULT 'STARTER'
        CHECK (plan IN ('STARTER', 'PRO', 'BUSINESS')),
    allowed_regions TEXT[] NOT NULL DEFAULT '{}',
    allowed_local_authorities TEXT[] NOT NULL DEFAULT '{}',
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS customer_users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id UUID NOT NULL REFERENCES customer_accounts(id) ON DELETE CASCADE,
    cognito_sub TEXT NOT NULL UNIQUE,
    email TEXT NOT NULL,
    display_name TEXT,
    role TEXT NOT NULL DEFAULT 'MEMBER' CHECK (role IN ('OWNER', 'MEMBER')),
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'SUSPENDED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (account_id, email)
);

CREATE INDEX IF NOT EXISTS customer_users_account_idx ON customer_users (account_id);

CREATE TABLE IF NOT EXISTS customer_saved_opportunities (
    customer_user_id UUID NOT NULL REFERENCES customer_users(id) ON DELETE CASCADE,
    opportunity_id UUID NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
    saved_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (customer_user_id, opportunity_id)
);

CREATE TABLE IF NOT EXISTS customer_saved_searches (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_user_id UUID NOT NULL REFERENCES customer_users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    criteria JSONB NOT NULL DEFAULT '{}'::jsonb,
    digest_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS customer_saved_searches_user_idx
    ON customer_saved_searches (customer_user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS customer_alert_preferences (
    customer_user_id UUID PRIMARY KEY REFERENCES customer_users(id) ON DELETE CASCADE,
    frequency TEXT NOT NULL DEFAULT 'WEEKLY'
        CHECK (frequency IN ('OFF', 'WEEKLY', 'DAILY', 'IMMEDIATE')),
    regions TEXT[] NOT NULL DEFAULT '{}',
    local_authorities TEXT[] NOT NULL DEFAULT '{}',
    change_types TEXT[] NOT NULL DEFAULT '{}',
    lifecycle_stages TEXT[] NOT NULL DEFAULT '{}',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS customer_usage_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id UUID NOT NULL REFERENCES customer_accounts(id) ON DELETE CASCADE,
    customer_user_id UUID NOT NULL REFERENCES customer_users(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL CHECK (event_type IN (
        'LOGIN', 'OPPORTUNITY_VIEWED', 'OPPORTUNITY_SAVED',
        'OPPORTUNITY_UNSAVED', 'SEARCH_USED', 'FILTER_USED',
        'SOURCE_LINK_CLICKED', 'DIGEST_OPENED', 'DIGEST_CLICKED'
    )),
    opportunity_id UUID REFERENCES opportunities(id) ON DELETE SET NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS customer_usage_events_account_idx
    ON customer_usage_events (account_id, created_at DESC);

CREATE TABLE IF NOT EXISTS customer_digest_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id UUID NOT NULL REFERENCES customer_accounts(id) ON DELETE CASCADE,
    customer_user_id UUID NOT NULL REFERENCES customer_users(id) ON DELETE CASCADE,
    frequency TEXT NOT NULL,
    period_start TIMESTAMPTZ NOT NULL,
    period_end TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PREVIEWED', 'QUEUED', 'SENT', 'FAILED')),
    opportunity_count INTEGER NOT NULL DEFAULT 0,
    safe_failure TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at TIMESTAMPTZ,
    UNIQUE (customer_user_id, frequency, period_start, period_end)
);

CREATE INDEX IF NOT EXISTS opportunities_customer_published_idx
    ON opportunities (latest_update_at DESC)
    WHERE vertical = 'CHILDRENS_HOME' AND publication_status = 'PUBLISHED';
