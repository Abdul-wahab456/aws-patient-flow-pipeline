-- Gold dimension: one row per hospital or clinic (an "organization" in FHIR).
CREATE TABLE IF NOT EXISTS pf_gold.dim_facility (
  organization_id string,
  name            string,
  type            string,
  city            string,
  state           string,
  postal_code     string
)
LOCATION 's3://{{LAKE_BUCKET}}/gold/dim_facility/'
TBLPROPERTIES ('table_type' = 'ICEBERG')
