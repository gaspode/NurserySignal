CREATE TABLE IF NOT EXISTS admin_audit_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_count INTEGER NOT NULL DEFAULT 0 CHECK (target_count >= 0),
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS admin_audit_events_created_at_idx
    ON admin_audit_events (created_at DESC);

CREATE INDEX IF NOT EXISTS admin_audit_events_action_idx
    ON admin_audit_events (action, created_at DESC);
