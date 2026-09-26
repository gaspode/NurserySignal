ALTER TABLE signal_ai_reviews
    ADD COLUMN IF NOT EXISTS planning_relevance TEXT
    CHECK (
        planning_relevance IS NULL
        OR planning_relevance IN ('RELEVANT_CHANGE', 'RELEVANT_FOLLOWUP', 'RELEVANT_ROUTINE', 'UNCERTAIN', 'IRRELEVANT')
    );
