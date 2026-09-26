ALTER TABLE opportunities ADD COLUMN IF NOT EXISTS creation_reason TEXT;

-- Legacy stage_reason values may contain a relationship explanation. Keep
-- those values intact, but give the opportunity a non-relational basis.
UPDATE opportunities
SET creation_reason = CASE
    WHEN stage_reason IS NULL OR stage_reason = ''
        THEN 'Created from preserved signal evidence.'
    WHEN lower(stage_reason) LIKE 'same postcode%'
        OR lower(stage_reason) LIKE '%compatible operator%'
        THEN 'Created from qualifying signal evidence; see linked signal reasons.'
    ELSE stage_reason
END
WHERE creation_reason IS NULL;
