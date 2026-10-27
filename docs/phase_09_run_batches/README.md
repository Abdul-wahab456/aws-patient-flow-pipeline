# Phase 9 — Run all 4 batches, first README

## Goal
Load every batch through the real trigger, one at a time, and check that the numbers are right.

## How (`notebooks/run_batches.ipynb`)
For each batch the notebook does what a sender does: it writes `landing/batch_NN/_SUCCESS` (with the file count) and waits. EventBridge starts the workflow; the notebook only watches the run. `release_batch` (in `infra/release_batch.py`, called through `infra/aws_helpers.py`) counts the files, writes the marker, finds the one run that started and waits for it. One batch at a time, because every run merges into the same Iceberg tables.

## Result
| Batch | Ingest date | Files | Run |
|---|---|---|---|
| 1 | 2026-11-01 | 37 | SUCCEEDED, 9.5 min |
| 2 | 2026-11-02 | 34 | SUCCEEDED, 8.3 min |
| 3 | 2026-11-03 | 34 | SUCCEEDED, 8.3 min |
| 4 | 2026-11-04 | 34 | SUCCEEDED, 7.0 min |

All four had been loaded before by hand, so this also shows that **loading a batch again is safe**: the counts did not grow.

| Check | Result |
|---|---|
| Gold tables | `dim_patient` 137, `dim_facility` 521, `dim_practitioner` 521, `dim_date` 47,847, `fact_encounter` 4,586 — equal to the source files |
| Encounters by class | AMB 4,313, EMER 127, IMP 114, VR 18, HH 14 |
| Data-quality checks | 14 checks × 4 batches, all 0 |
| KPI views | 114 inpatient stays, **17 readmissions within 30 days**, 127 emergency visits |

Where the 8 minutes go: Ingest a few seconds, Glue about 3 minutes (starting Spark is most of it), each of the four merges about one minute because the Step Functions Athena integration asks "finished?" only about once a minute, and the quality step one more minute.

## The README
`README.md` (project root): what the project is, the architecture as a Mermaid diagram, the data model, the KPI charts (`docs/images/`), how to run, cost, lessons learned.
