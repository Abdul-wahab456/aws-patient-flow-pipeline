-- Referential integrity: every encounter must point to a doctor in dim_practitioner (by NPI).
SELECT 'gold_fact_without_practitioner' AS check_name, COUNT(*) AS failures
FROM pf_gold.fact_encounter AS f
LEFT JOIN pf_gold.dim_practitioner AS d ON f.practitioner_npi = d.npi
WHERE d.npi IS NULL
