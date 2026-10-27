-- Gold fact: one row per encounter (a visit or stay). Partitioned by the YEAR of the start time.
-- (Month would not work: the visits span 1924-2026, up to 352 months in one batch, and Athena allows at most
--  100 open partition writers per query. A year per partition also avoids hundreds of tiny files.)
-- los = length of stay. All times are UTC.
CREATE TABLE IF NOT EXISTS pf_gold.fact_encounter (
  encounter_id           string,
  patient_id             string,
  organization_id        string,
  practitioner_npi       string,
  encounter_class        string,
  start_ts               timestamp,
  end_ts                 timestamp,
  start_date             date,
  end_date               date,
  los_hours              double,
  los_days               double,
  is_inpatient           boolean,
  is_emergency           boolean,
  reason_code            string,
  reason_display         string,
  primary_condition_code string
)
PARTITIONED BY (year(start_ts))
LOCATION 's3://{{LAKE_BUCKET}}/gold/fact_encounter/'
TBLPROPERTIES ('table_type' = 'ICEBERG')
