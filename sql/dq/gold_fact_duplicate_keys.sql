-- An encounter must appear once in the fact table. failures = surplus rows.
SELECT 'gold_fact_duplicate_keys' AS check_name, COUNT(*) - COUNT(DISTINCT encounter_id) AS failures
FROM pf_gold.fact_encounter
