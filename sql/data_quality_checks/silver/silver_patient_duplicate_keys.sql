-- A patient id must appear once in a batch. failures = number of surplus rows.
SELECT 'silver_patient_duplicate_keys' AS check_name, COUNT(*) - COUNT(DISTINCT patient_id) AS failures
FROM pf_silver.patient WHERE ingest_date = (SELECT ingest_date FROM params)
