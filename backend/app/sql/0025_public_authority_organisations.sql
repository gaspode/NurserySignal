ALTER TABLE operators
    ADD COLUMN IF NOT EXISTS organisation_type TEXT NOT NULL DEFAULT 'UNKNOWN',
    ADD COLUMN IF NOT EXISTS organisation_type_source TEXT,
    ADD COLUMN IF NOT EXISTS organisation_type_updated_at TIMESTAMPTZ;

ALTER TABLE operators DROP CONSTRAINT IF EXISTS operators_organisation_type_check;
ALTER TABLE operators ADD CONSTRAINT operators_organisation_type_check
    CHECK (organisation_type IN ('PRIVATE_COMPANY', 'PUBLIC_AUTHORITY', 'UNKNOWN', 'OTHER'));

UPDATE operators
SET organisation_type = 'PRIVATE_COMPANY',
    organisation_type_source = COALESCE(organisation_type_source, 'EXISTING_COMPANY_NUMBER'),
    organisation_type_updated_at = COALESCE(organisation_type_updated_at, now())
WHERE companies_house_number IS NOT NULL
  AND organisation_type = 'UNKNOWN';

CREATE INDEX IF NOT EXISTS operators_organisation_type_idx
    ON operators (organisation_type, updated_at DESC);
