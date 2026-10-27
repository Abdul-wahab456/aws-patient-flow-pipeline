-- Gold dimension: one row per doctor. The key is the NPI, because encounters reference doctors by NPI.
CREATE TABLE IF NOT EXISTS pf_gold.dim_practitioner (
  npi             string,
  practitioner_id string,
  full_name       string,
  gender          string
)
LOCATION 's3://{{LAKE_BUCKET}}/gold/dim_practitioner/'
TBLPROPERTIES ('table_type' = 'ICEBERG')
