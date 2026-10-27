-- Referential integrity: every encounter must point to a patient in dim_patient.
SELECT 'gold_fact_without_patient' AS check_name, COUNT(*) AS failures
FROM pf_gold.fact_encounter AS f
LEFT JOIN pf_gold.dim_patient AS d ON f.patient_id = d.patient_id
WHERE d.patient_id IS NULL
