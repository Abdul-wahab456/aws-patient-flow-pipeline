-- A condition id must appear once in a batch. failures = number of surplus rows.
SELECT 'silver_condition_duplicate_keys' AS check_name, COUNT(*) - COUNT(DISTINCT condition_id) AS failures
FROM pf_silver.condition WHERE ingest_date = (SELECT ingest_date FROM params)
