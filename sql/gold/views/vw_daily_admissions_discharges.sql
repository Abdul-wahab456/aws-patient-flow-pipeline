-- KPI: hospital admissions and discharges per day (inpatient stays only).
-- An admission is counted on the day the stay starts, a discharge on the day it ends.
CREATE OR REPLACE VIEW pf_gold.vw_daily_admissions_discharges AS
WITH admissions AS (
  SELECT start_date AS date_key, COUNT(*) AS admissions
  FROM pf_gold.fact_encounter
  WHERE is_inpatient
  GROUP BY start_date
),
discharges AS (
  SELECT end_date AS date_key, COUNT(*) AS discharges
  FROM pf_gold.fact_encounter
  WHERE is_inpatient AND end_date IS NOT NULL
  GROUP BY end_date
)
SELECT
  COALESCE(a.date_key, d.date_key) AS date_key,
  COALESCE(a.admissions, 0) AS admissions,
  COALESCE(d.discharges, 0) AS discharges
FROM admissions AS a
FULL OUTER JOIN discharges AS d ON a.date_key = d.date_key
