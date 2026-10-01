CREATE TABLE IF NOT EXISTS mcp_request_audit (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    argument_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    success BOOLEAN,
    error_code TEXT,
    duration_ms INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS mcp_request_audit_client_created_idx
    ON mcp_request_audit (client_id, created_at DESC);

CREATE INDEX IF NOT EXISTS mcp_request_audit_created_idx
    ON mcp_request_audit (created_at DESC);

