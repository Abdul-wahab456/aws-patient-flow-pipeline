-- KPI: average and median length of stay (in days) of inpatient stays, per facility and month.
-- A stay belongs to the month in which it started.
CREATE OR REPLACE VIEW pf_gold.vw_avg_los_by_facility_month AS
SELECT
  date_trunc('month', f.start_date) AS month,
  f.organization_id,
  d.name AS facility_name,
  COUNT(*) AS inpatient_stays,
  AVG(f.los_days) AS avg_los_days,
  approx_percentile(f.los_days, 0.5) AS median_los_days
FROM pf_gold.fact_encounter AS f
LEFT JOIN pf_gold.dim_facility AS d ON f.organization_id = d.organization_id
WHERE f.is_inpatient
GROUP BY date_trunc('month', f.start_date), f.organization_id, d.name
