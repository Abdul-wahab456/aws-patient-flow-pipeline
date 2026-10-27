-- KPI: the reasons encounters happen, ranked by how many encounters have them.
-- Only encounters that record a reason are counted. rank_by_volume 1 = the most frequent reason.
CREATE OR REPLACE VIEW pf_gold.vw_top_reasons AS
SELECT
  reason_code,
  reason_display,
  COUNT(*) AS encounters,
  COUNT(DISTINCT patient_id) AS patients,
  RANK() OVER (ORDER BY COUNT(*) DESC) AS rank_by_volume
FROM pf_gold.fact_encounter
WHERE reason_code IS NOT NULL
GROUP BY reason_code, reason_display
