-- Gold dimension: one row per patient. Updated in place by merge_dim_patient.sql (SCD type 1: the newest values win).
-- Iceberg table. In CREATE TABLE, Athena uses Hive type names (string, int, double, boolean, date, timestamp).
CREATE TABLE IF NOT EXISTS pf_gold.dim_patient (
  patient_id        string,
  gender            string,
  birth_date        date,
  deceased_datetime timestamp,
  is_deceased       boolean,
  marital_status    string,
  city              string,
  state             string,
  postal_code       string,
  age_years         int,
  age_group         string
)
LOCATION 's3://{{LAKE_BUCKET}}/gold/dim_patient/'
TBLPROPERTIES ('table_type' = 'ICEBERG')
