-- Preserve failed attempts while allowing the same bounded benchmark parameters
-- to be retried. Successful or currently running fingerprints remain idempotent.
ALTER TABLE backtest_runs
    DROP CONSTRAINT IF EXISTS backtest_runs_run_fingerprint_key;

CREATE UNIQUE INDEX IF NOT EXISTS backtest_runs_active_fingerprint_idx
    ON backtest_runs (run_fingerprint)
    WHERE status IN ('RUNNING', 'SUCCESS');
