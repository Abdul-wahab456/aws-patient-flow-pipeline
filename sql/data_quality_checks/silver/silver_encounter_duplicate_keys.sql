-- An encounter id must appear once in a batch. failures = number of surplus rows.
SELECT 'silver_encounter_duplicate_keys' AS check_name, COUNT(*) - COUNT(DISTINCT encounter_id) AS failures
FROM pf_silver.encounter WHERE ingest_date = (SELECT ingest_date FROM params)
