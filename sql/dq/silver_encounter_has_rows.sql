-- The batch must contain encounters (fails when there are none).
SELECT 'silver_encounter_has_rows' AS check_name, CASE WHEN COUNT(*) = 0 THEN 1 ELSE 0 END AS failures
FROM pf_silver.encounter WHERE ingest_date = (SELECT ingest_date FROM params)
