-- Short-lived OAuth authorization state for the ChatGPT CIMD facade.
-- This is operational authentication state only; it never mutates SignalHub business records.
CREATE TABLE IF NOT EXISTS mcp_oauth_transactions (
    state TEXT PRIMARY KEY,
    original_state TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    client_id TEXT NOT NULL,
    resource TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS mcp_oauth_transactions_expires_idx
    ON mcp_oauth_transactions (expires_at);
