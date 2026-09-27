-- Preserve the exact candidate set behind an organisation-resolution decision.
-- A rejected set is only offered again when materially different candidates appear.
ALTER TABLE organisation_match_reviews
    ADD COLUMN IF NOT EXISTS candidate_fingerprint CHAR(64);

CREATE INDEX IF NOT EXISTS organisation_match_reviews_rejected_candidates_idx
    ON organisation_match_reviews (operator_id, provider, candidate_fingerprint)
    WHERE status = 'REJECTED' AND candidate_fingerprint IS NOT NULL;
