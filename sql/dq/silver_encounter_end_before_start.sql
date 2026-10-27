-- A visit cannot end before it starts.
SELECT 'silver_encounter_end_before_start' AS check_name, COUNT(*) AS failures
FROM pf_silver.encounter WHERE ingest_date = (SELECT ingest_date FROM params) AND end_ts < start_ts
