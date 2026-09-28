-- Public CareProspect pilot enquiries are kept separate from customer accounts.
-- Submission is bounded and idempotent; only authenticated SignalHub admins can list them.
CREATE TABLE IF NOT EXISTS customer_access_requests (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    company TEXT NOT NULL,
    work_email TEXT NOT NULL,
    supplier_category TEXT NOT NULL,
    message TEXT,
    status TEXT NOT NULL DEFAULT 'NEW' CHECK (status IN ('NEW', 'CONTACTED', 'CLOSED')),
    request_fingerprint CHAR(64) NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_submitted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS customer_access_requests_status_created_idx
    ON customer_access_requests (status, created_at DESC);
