# Phase 5 — Glue job: bronze to silver

## Goal
Turn the raw FHIR files of one batch (bronze) into five clean, typed Parquet tables (silver) that Athena can query. This is where the transformation happens: the nested JSON is flattened, references become plain ids, and text becomes real dates and timestamps.

```
bronze/patient_records/ingest_date=2026-11-01/*.json                 silver/<table>/ingest_date=2026-11-01/*.parquet
   35 patient bundles  ------------------------------->  patient, encounter, condition
    1 hospital file    ------------------------------->  organization
    1 practitioner file ------------------------------>  practitioner
                              (Glue job pf-bronze-to-silver, Spark 3.5.4)        + tables registered in Glue database pf_silver
```

## What this phase creates
| Item | Purpose |
|---|---|
| `glue_jobs/fhir_utils.py` | The transformation logic: schemas, reference resolver, the five table builders, checks, DDL builder. Needs no AWS, so it is tested on the laptop. |
| `glue_jobs/bronze_to_silver.py` | The job: lists the bronze files, builds and checks the tables, writes each partition, registers it in the Glue catalog |
| `tests/test_fhir_utils.py` | 20 tests that run Spark locally on small sample resources |
| `infra/aws_helpers.py` additions | `run_glue_job()` (start a run, wait, return the result) |
| `notebooks/bronze_to_silver_job.ipynb` | Uploads the scripts, creates the job, runs it, and checks the result against the source files |
| AWS: Glue job `pf-bronze-to-silver` | Glue 5.0 (Spark 3.5.4, Python 3.11), 2 × G.1X workers, 10-minute timeout, 0 retries |
| AWS: 5 tables in `pf_silver` | `patient`, `encounter`, `condition`, `organization`, `practitioner` (partitioned by `ingest_date`) |

## Concepts
- **Glue job**: a Spark program that AWS runs on temporary machines. You pay for the minutes it runs (DPU-seconds), and nothing when it is idle.
- **DPU** (Data Processing Unit): 4 vCPU and 16 GB. Two G.1X workers = 2 DPU.
- **Spark DataFrame**: a table of data that Spark processes in parallel; transformations are lazy and only run when a result is needed.
- **Flatten / explode**: a FHIR bundle holds a list of resources; `explode` turns each list item into its own row.
- **Parquet**: a compressed, column-oriented file format, much faster and smaller than JSON for queries.
- **Partition**: a folder `ingest_date=2026-11-01/` per batch. Athena reads the folder name as a column and can skip folders it does not need.
- **Glue Data Catalog table**: the definition (columns, types, location) that lets Athena find and read the Parquet files.
- **Idempotent**: running the same batch again produces the same tables, with nothing doubled.

## How the transformation works (`fhir_utils.py`)
1. **Read each resource as text.** A bundle file is read with a tiny schema (`entry[].resource` as a string), so every resource arrives as one row holding its JSON text.
   *Why:* FHIR reuses field names with different shapes. `Claim.type` is an object, `Encounter.type` is a list. One combined schema would fail on such files. Reading as text and parsing each type separately avoids that (a unit test covers exactly this).
2. **Pick the type** with `get_json_object(json, '$.resourceType')`.
3. **Parse with that type's own schema** (`from_json`): only the fields we keep are listed; the rest of the JSON is ignored, which also saves memory and time.
4. **Select, rename, cast**: `F.to_date`, `F.to_timestamp`, `[0]` for the first item of a list.
5. **Resolve references** with one Spark expression (`resolve_reference`): `urn:uuid:<id>` → `<id>`, and `Organization?identifier=<system>|<id>` → `<id>`.
6. **Deduplicate** on the primary key (`dropDuplicates`).

## The five silver tables
Partition column `ingest_date` (string) is the folder name, not a column inside the files.

| Table | Columns |
|---|---|
| `patient` | patient_id, gender, birth_date *(date)*, deceased_datetime *(timestamp)*, marital_status, city, state, postal_code |
| `encounter` | encounter_id, patient_id, status, encounter_class, type_code, type_display, start_ts, end_ts *(timestamps)*, organization_id, **practitioner_npi**, reason_code, reason_display |
| `condition` | condition_id, patient_id, encounter_id, code, display, clinical_status, onset_ts, abatement_ts *(timestamps)* |
| `organization` | organization_id, name, type, city, state, postal_code |
| `practitioner` | practitioner_id, npi, full_name, gender |

### Decisions made in this phase
| Decision | Why |
|---|---|
| **Encounter column `practitioner_npi`** (the plan said `practitioner_id`) | Encounters reference the doctor by NPI, never by the Practitioner's own id (found in the data analysis: 100% join by NPI, 0% by uuid). The name now says what the value is. The gold layer will join `encounter.practitioner_npi` to `practitioner.npi`. |
| **Timestamps stored as UTC instants** | The source has mixed offsets (`+05:00`, `+06:00`, `+05:30`, `+06:30`). `1997-09-04T08:10:14+05:00` is stored as `03:10:14 UTC`; the job sets `spark.sql.session.timeZone=UTC` and writes `TIMESTAMP_MICROS`, which Athena reads as `timestamp`. Hour-of-day KPIs will therefore be in UTC. |
| **Reference tables only written when the files exist** | The hospital and practitioner lists arrive only with batch 1. A batch without them leaves `organization` and `practitioner` untouched. |
| **Nothing is written if a check fails** | All tables are built, cached and checked first (required columns not null, patient and encounter not empty); only then are partitions written. |
| **`CREATE TABLE IF NOT EXISTS`** | Creating a table that exists changes nothing. If a column is ever added, the table has to be altered or recreated: noted for the future. |
| **No lineage columns** `[EXTRA]` | A `source_file` column would show which bundle each row came from. Not added to keep silver as specified; easy to add later. |

## How the job writes (`bronze_to_silver.py`)
1. Validate `--ingest_date` (real date, `YYYY-MM-DD`).
2. List `bronze/patient_records/ingest_date=<date>/` with boto3 and split the files by name (`classify_bronze_files`: patient bundles, hospital file, practitioner file; ignores `_batch_metadata.json`). **No patient files → fail** with "was the batch ingested?".
3. Build the tables, then check them (`require_no_nulls`, row counts).
4. For each table: `df.write.mode("overwrite").parquet(".../silver/<table>/ingest_date=<date>/")`, read it back and compare the row count, run `CREATE EXTERNAL TABLE IF NOT EXISTS` (from the DataFrame's own schema, so code and table cannot drift apart), `ALTER TABLE ... ADD IF NOT EXISTS PARTITION`, then confirm with `SHOW PARTITIONS` that the partition is in the Glue catalog. A job that was not really connected to the catalog would fail here instead of silently doing nothing.
5. Print `ROWCOUNT table=... rows=...` lines (visible in CloudWatch Logs).

Idempotency comes from **overwriting the partition folder**: a rerun deletes the old files and writes new ones, and the partition is registered only if missing.

## Glue job settings
| Setting | Value | Why |
|---|---|---|
| Glue version | 5.0 | Spark 3.5.4, Python 3.11, Java 17. Same Spark version as the local tests (pyspark 3.5.4). Newer versions exist (5.1: Spark 3.5.6; 6.0: Spark 4.1.1); 5.0 was chosen to match the plan and the tested setup. |
| Workers | 2 × G.1X | smallest sensible size (the plan's cost guardrail) |
| Timeout / retries | 10 min / 0 | a stuck or failing run cannot keep billing |
| `--enable-glue-datacatalog` | true | lets `spark.sql` create tables in the Glue Data Catalog |
| `--extra-py-files` | `fhir_utils.py` on S3 | makes `import fhir_utils` work in the job |
| Arguments | `--ingest_date`, `--lake_bucket`, `--silver_database` | all validated or required |

Commands in the notebook: `aws s3 cp` (scripts to `glue_scripts/`), `aws glue create-job` / `update-job`, then runs through `aws glue start-job-run` and `get-job-run` (wrapped by `run_glue_job`).

## Tests on the laptop (`tests/test_fhir_utils.py`, 20 tests)
Cover: date validation; splitting bronze files by name; DDL text; reference resolution (both styles, null); reading bundles whose resources have different shapes; the null check; each of the five tables (values, types, UTC conversion, missing optional fields become null, NPI picked by system, name without a stray space); duplicates kept once; required columns present. `fhir_utils.py` coverage: **100%**.

Two local gotchas (both fixed, worth remembering):
1. **A folder path with a space** (`D:\Visual Code\...`) stops Spark on Windows from starting its Python helper process. The tests therefore build their sample DataFrames by writing JSON to a temp file and reading it back as text, which is also what the real job does.
2. **`collect()` converts timestamps to the computer's own time zone.** A correct `03:10 UTC` came back as `08:10` on a laptop in UTC+5, which would have made the tests pass on GitHub and fail locally. The tests now format timestamps inside Spark, in UTC.

## Tests on AWS (`bronze_to_silver_job.ipynb`)
| Test | Result |
|---|---|
| Job run, batch 1 | `SUCCEEDED`, 121 s, 242 DPU-seconds ≈ **$0.03** |
| Row counts vs the source files (counted with plain Python) | patient 35, encounter 1,411, condition 854, organization 521, practitioner 521: all equal |
| Ids vs the source | the set of ids in each table is identical to the source's |
| Column types in Athena | dates, timestamps and `ingest_date` as designed |
| One inpatient encounter, field by field | all 8 checked columns equal what plain Python derives from the source (including start/end converted to UTC) |
| Keys that must join | 0 encounters without a patient, organization or practitioner (by NPI); 0 conditions without an encounter |
| **Rerun of batch 1** | `SUCCEEDED`; same counts; every partition's files come from the new run only (one write id per partition, different from the first run's) |
| **Batch 2** (no hospital or practitioner files) | `SUCCEEDED`; patient 34, encounter 870, condition 612 equal the source; organization and practitioner still hold only batch 1's partition |
| **Batch never ingested** (2026-11-09) | `FAILED` in 47 s with `no patient files under s3://…/ingest_date=2026-11-09/: was the batch ingested?`; nothing written |
| Job log | `ROWCOUNT` lines found in CloudWatch Logs |

Cost of this phase's 4 runs: about $0.12 in total. One run takes about 2 minutes, mostly job startup and the five writes.

Note on the first notebook run: the "run again" check first compared the number of objects in `silver/` and failed, because the Glue file system also creates empty `<table>_$folder$` placeholder objects next to the table folders. The data was correct; the check was changed to compare the write id in the Parquet file names, which proves the old files are gone and the new ones are complete.

## Not in this phase
- Batches 3 and 4 have not been ingested or transformed yet; the pipeline will run them (Phase 7/9).
- The job script in S3 is the unversioned one deployed by the notebook; GitHub Actions (`docs/cd_github_actions.md`) deploys versioned copies after a push.

## Done when
- All 5 silver tables are queryable in Athena for batch 1. ✅
- A rerun gives the same row counts. ✅
