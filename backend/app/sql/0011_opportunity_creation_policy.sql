ALTER TABLE opportunities ADD COLUMN IF NOT EXISTS change_type TEXT NOT NULL DEFAULT 'OTHER_CHANGE';
ALTER TABLE opportunities DROP CONSTRAINT IF EXISTS opportunities_change_type_check;
ALTER TABLE opportunities ADD CONSTRAINT opportunities_change_type_check
    CHECK (change_type IN ('OPENING', 'EXPANSION', 'RELOCATION', 'OTHER_CHANGE'));

CREATE INDEX IF NOT EXISTS opportunities_change_type_idx ON opportunities (change_type);
