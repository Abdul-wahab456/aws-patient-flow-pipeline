-- Referential integrity: the start date of every encounter must exist in dim_date.
SELECT 'gold_fact_without_date' AS check_name, COUNT(*) AS failures
FROM pf_gold.fact_encounter AS f
LEFT JOIN pf_gold.dim_date AS d ON f.start_date = d.date_key
WHERE d.date_key IS NULL
