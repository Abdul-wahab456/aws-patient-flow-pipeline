-- Upsert the organizations of ONE batch (the ? is the ingest_date) into gold.dim_facility.
-- Only batch 1 carries the hospital file, so for the other batches the source is empty and nothing changes.
MERGE INTO pf_gold.dim_facility AS t
USING (
  SELECT organization_id, name, type, city, state, postal_code
  FROM (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY organization_id ORDER BY name) AS rn
    FROM pf_silver.organization
    WHERE ingest_date = ?
  )
  WHERE rn = 1
) AS s
ON t.organization_id = s.organization_id
WHEN MATCHED THEN UPDATE SET
  name = s.name, type = s.type, city = s.city, state = s.state, postal_code = s.postal_code
WHEN NOT MATCHED THEN INSERT (organization_id, name, type, city, state, postal_code)
VALUES (s.organization_id, s.name, s.type, s.city, s.state, s.postal_code)
