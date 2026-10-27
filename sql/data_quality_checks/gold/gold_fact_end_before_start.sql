-- A visit cannot end before it starts (checked on the gold fact table).
SELECT 'gold_fact_end_before_start' AS check_name, COUNT(*) AS failures
FROM pf_gold.fact_encounter WHERE end_ts < start_ts
