-- Phase B2 remains preview-only. Persist the deterministic watcher policy
-- version in the schema contract for any later, separately approved enrollment.
ALTER TABLE planning_lifecycle_watches
    ALTER COLUMN policy_version SET DEFAULT 'care-planning-watcher-v2';

COMMENT ON COLUMN planning_lifecycle_watches.policy_version IS
    'Versioned deterministic eligibility/cadence policy; enrollment remains separately gated.';
