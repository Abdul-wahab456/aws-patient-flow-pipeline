-- KPI: 30-day readmissions.
-- An inpatient stay is a readmission if it starts within 30 days after the end of the SAME patient's previous
-- inpatient stay (LAG looks at the previous stay when the patient's stays are ordered by start time).
-- Stays that overlap in time are not readmissions. The rate counts a readmission in the month and at the
-- facility where it started: readmission_rate = readmissions_30d / inpatient_admissions.
CREATE OR REPLACE VIEW pf_gold.vw_readmission_30d AS
WITH inpatient AS (
  SELECT
    encounter_id, patient_id, organization_id, start_ts,
    LAG(end_ts) OVER (PARTITION BY patient_id ORDER BY start_ts) AS previous_discharge_ts
  FROM pf_gold.fact_encounter
  WHERE is_inpatient
),
flagged AS (
  SELECT
    *,
    previous_discharge_ts IS NOT NULL
      AND start_ts >= previous_discharge_ts
      AND start_ts <= previous_discharge_ts + INTERVAL '30' DAY AS is_readmission_30d
  FROM inpatient
)
SELECT
  date_trunc('month', CAST(f.start_ts AS date)) AS month,
  f.organization_id,
  d.name AS facility_name,
  COUNT(*) AS inpatient_admissions,
  SUM(CASE WHEN f.is_readmission_30d THEN 1 ELSE 0 END) AS readmissions_30d,
  CAST(SUM(CASE WHEN f.is_readmission_30d THEN 1 ELSE 0 END) AS double) / COUNT(*) AS readmission_rate
FROM flagged AS f
LEFT JOIN pf_gold.dim_facility AS d ON f.organization_id = d.organization_id
GROUP BY date_trunc('month', CAST(f.start_ts AS date)), f.organization_id, d.name
