CREATE TABLE IF NOT EXISTS signal_ai_reviews (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    raw_signal_id UUID NOT NULL REFERENCES raw_signals(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    model_id TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    recommendation TEXT CHECK (recommendation IN ('APPROVE', 'REJECT', 'NEEDS_HUMAN')),
    confidence NUMERIC(5, 4) CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    reason TEXT,
    status TEXT NOT NULL CHECK (status IN ('SUCCEEDED', 'FAILED')),
    failure_category TEXT,
    attempted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    evaluated_at TIMESTAMPTZ,
    input_tokens INTEGER CHECK (input_tokens IS NULL OR input_tokens >= 0),
    output_tokens INTEGER CHECK (output_tokens IS NULL OR output_tokens >= 0),
    latency_ms INTEGER CHECK (latency_ms IS NULL OR latency_ms >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (raw_signal_id, provider, model_id, prompt_version)
);

CREATE INDEX IF NOT EXISTS signal_ai_reviews_signal_idx ON signal_ai_reviews (raw_signal_id, created_at DESC);
