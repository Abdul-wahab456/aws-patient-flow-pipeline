-- Upsert the patients of ONE batch (the ? is the ingest_date) into gold.dim_patient.
--   known patient   -> its row is updated (SCD type 1: the newest values replace the old)
--   new patient     -> a row is inserted
-- ROW_NUMBER keeps one source row per patient, so no target row can be matched twice.
-- Running it again for the same batch changes nothing: no duplicates.
-- age_years is the age on the day the MERGE runs; age_group buckets it.
MERGE INTO pf_gold.dim_patient AS t
USING (
  SELECT
    patient_id, gender, birth_date, deceased_datetime,
    deceased_datetime IS NOT NULL AS is_deceased,
    marital_status, city, state, postal_code,
    CAST(date_diff('year', birth_date, current_date) AS integer) AS age_years,
    CASE
      WHEN date_diff('year', birth_date, current_date) < 18 THEN '0-17'
      WHEN date_diff('year', birth_date, current_date) < 35 THEN '18-34'
      WHEN date_diff('year', birth_date, current_date) < 50 THEN '35-49'
      WHEN date_diff('year', birth_date, current_date) < 65 THEN '50-64'
      ELSE '65+'
    END AS age_group
  FROM (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY patient_id ORDER BY ingest_date DESC) AS rn
    FROM pf_silver.patient
    WHERE ingest_date = ?
  )
  WHERE rn = 1
) AS s
ON t.patient_id = s.patient_id
WHEN MATCHED THEN UPDATE SET
  gender = s.gender, birth_date = s.birth_date, deceased_datetime = s.deceased_datetime,
  is_deceased = s.is_deceased, marital_status = s.marital_status, city = s.city, state = s.state,
  postal_code = s.postal_code, age_years = s.age_years, age_group = s.age_group
WHEN NOT MATCHED THEN INSERT (
  patient_id, gender, birth_date, deceased_datetime, is_deceased, marital_status, city, state,
  postal_code, age_years, age_group
) VALUES (
  s.patient_id, s.gender, s.birth_date, s.deceased_datetime, s.is_deceased, s.marital_status, s.city, s.state,
  s.postal_code, s.age_years, s.age_group
)
