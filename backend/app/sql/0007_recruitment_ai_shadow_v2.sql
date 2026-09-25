ALTER TABLE signal_ai_reviews
    ADD COLUMN IF NOT EXISTS commercial_change_evidence TEXT
    CHECK (commercial_change_evidence IS NULL OR commercial_change_evidence IN ('NONE', 'WEAK', 'STRONG'));
