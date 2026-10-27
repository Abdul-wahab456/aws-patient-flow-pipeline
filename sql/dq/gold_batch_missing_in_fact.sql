-- Every silver encounter of the batch must have reached the gold fact table.
SELECT 'gold_batch_missing_in_fact' AS check_name, COUNT(*) AS failures
FROM pf_silver.encounter AS s
LEFT JOIN pf_gold.fact_encounter AS f ON s.encounter_id = f.encounter_id
WHERE s.ingest_date = (SELECT ingest_date FROM params) AND f.encounter_id IS NULL
