-- A patient must appear once in the patient dimension. failures = surplus rows.
SELECT 'gold_dim_patient_duplicate_keys' AS check_name, COUNT(*) - COUNT(DISTINCT patient_id) AS failures
FROM pf_gold.dim_patient
