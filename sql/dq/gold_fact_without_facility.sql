-- Referential integrity: every encounter must point to a facility in dim_facility.
SELECT 'gold_fact_without_facility' AS check_name, COUNT(*) AS failures
FROM pf_gold.fact_encounter AS f
LEFT JOIN pf_gold.dim_facility AS d ON f.organization_id = d.organization_id
WHERE d.organization_id IS NULL
