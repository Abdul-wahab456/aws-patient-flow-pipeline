-- Upsert the doctors of ONE batch (the ? is the ingest_date) into gold.dim_practitioner, keyed on the NPI.
-- Only batch 1 carries the practitioner file, so for the other batches the source is empty and nothing changes.
MERGE INTO pf_gold.dim_practitioner AS t
USING (
  SELECT npi, practitioner_id, full_name, gender
  FROM (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY npi ORDER BY practitioner_id) AS rn
    FROM pf_silver.practitioner
    WHERE ingest_date = ?
  )
  WHERE rn = 1
) AS s
ON t.npi = s.npi
WHEN MATCHED THEN UPDATE SET
  practitioner_id = s.practitioner_id, full_name = s.full_name, gender = s.gender
WHEN NOT MATCHED THEN INSERT (npi, practitioner_id, full_name, gender)
VALUES (s.npi, s.practitioner_id, s.full_name, s.gender)
