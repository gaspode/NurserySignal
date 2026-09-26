ALTER TABLE signal_ai_reviews
    ADD COLUMN IF NOT EXISTS recruitment_relevance TEXT
    CHECK (
        recruitment_relevance IS NULL
        OR recruitment_relevance IN ('RELEVANT_ROUTINE', 'RELEVANT_CHANGE', 'UNCERTAIN', 'IRRELEVANT')
    );
