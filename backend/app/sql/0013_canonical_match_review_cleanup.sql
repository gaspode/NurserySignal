-- Distinguish consolidation history from an actual rejected relationship.
ALTER TABLE opportunity_signals DROP CONSTRAINT IF EXISTS opportunity_signals_status_check;
ALTER TABLE opportunity_signals ADD CONSTRAINT opportunity_signals_status_check
    CHECK (status IN ('ACTIVE', 'REJECTED', 'SUPERSEDED'));

-- Existing system consolidation rows were previously represented as REJECTED.
-- Keep admin rejections untouched; only convert the explicit system wording.
UPDATE opportunity_signals
SET status = 'SUPERSEDED'
WHERE status = 'REJECTED'
  AND (
      match_reason LIKE 'consolidated into %'
      OR match_reason LIKE 'duplicate relationship consolidated into %'
  )
  AND COALESCE(created_by, 'SYSTEM') = 'SYSTEM';

ALTER TABLE opportunity_match_reviews DROP CONSTRAINT IF EXISTS opportunity_match_reviews_status_check;
ALTER TABLE opportunity_match_reviews ADD CONSTRAINT opportunity_match_reviews_status_check
    CHECK (status IN ('PENDING', 'LINKED', 'REJECTED', 'SUPERSEDED'));

CREATE INDEX IF NOT EXISTS opportunity_match_reviews_pending_candidate_idx
    ON opportunity_match_reviews (opportunity_id, status)
    WHERE status = 'PENDING';
