# Phase 4 — Ingest Lambda (bronze layer)

## Goal

> **Naming change (2026-10-26):** the target folder is `bronze/patient_records/ingest_date=<date>/` (was `raw/fhir/...`) and the receipt file is `_batch_metadata.json` (was `_manifest.json`). The Lambda, its unit tests and its IAM policy were updated, and the notebook was re-run.
>
> **Scope change (2026-10-26):** the project first used 4 batches, with `NUMBER_OF_BATCHES` = 4.
>
> **Update (2026-10-27): no fixed number of batches.** The setting `NUMBER_OF_BATCHES` is gone. The Lambda accepts any batch from 1 to 99 (a two-digit folder `batch_NN`); batch 0, batch 100 and `batch_5` are rejected. A new batch needs no code change, no setting and no deployment (see `docs/phase_10_event_trigger`). The text below describes the first version.

Move one batch of raw files from `landing/` into the **bronze layer**, untouched, under a folder named after its arrival date. This is the first step of every pipeline run.

```
s3://<lake>/landing/batch_01/*.json   --pf-ingest-->   s3://<lake>/bronze/patient_records/ingest_date=2026-11-01/*.json
                                                                                   + _batch_metadata.json
```

## What this phase creates
| Item | Purpose |
|---|---|
| `lambda/ingest/handler.py` | The function code |
| `tests/test_ingest_handler.py` | 14 unit tests that run on the laptop without AWS |
| `notebooks/ingest_lambda.ipynb` | Packages, deploys and tests the function in AWS |
| AWS Lambda `pf-ingest` | Python 3.12, 256 MB, 60 s timeout, role `pf-lambda-role` |
| `.gitignore` rule `build/` | The zip and the test output written by the notebook are never committed |

## Concepts
- **Lambda**: AWS runs your function on demand; you pay per run (a few runs a day is free). No server to manage.
- **Handler**: `handler.lambda_handler` means "file `handler.py`, function `lambda_handler`". AWS calls it with an `event` (the input) and a `context`.
- **Runtime**: the language environment AWS provides (`python3.12`). It already includes `boto3`, so the zip holds only our file.
- **Execution role**: the identity the function runs as. `pf-lambda-role` can read `landing/`, write `bronze/`, and write logs, and nothing else.
- **Environment variables**: settings stored with the function (bucket name, first ingest date, number of batches), so no name is hard-coded in the code.
- **Server-side copy** (`copy_object`): S3 copies the object internally. The 110 MB of a batch never passes through the Lambda, so memory use stayed at 93 MB of 256 MB.
- **Idempotent**: running the same batch twice leaves exactly the same result. Raw file names equal landing file names, so a rerun overwrites the same keys instead of adding new ones.
- **Bronze layer**: the data exactly as it arrived, only moved. Cleaning happens in the next phase.

## The code (`handler.py`)
The AWS-specific code is kept in small functions so the logic can be tested without AWS.

| Function | What it does |
|---|---|
| `parse_batch(event, n)` | Returns the batch number. Raises `ValueError` if `batch` is missing, not an integer (`True` and `"1"` are rejected too) or outside 1..4 (`NUMBER_OF_BATCHES`) |
| `ingest_date_for(batch, first_date)` | `2026-11-01 + (batch - 1) days`: one simulated arrival day per batch |
| `list_objects(s3, bucket, prefix)` | Lists all files under a prefix, page by page (S3 returns at most 1000 keys per page) |
| `ingest_batch(...)` | Lists the batch, **fails if it is empty**, copies each file, writes `_batch_metadata.json`, returns a summary |
| `lambda_handler(event, context)` | Entry point: reads the settings, runs the steps, logs one JSON line per step, re-raises any error so the caller sees the failure |

`_batch_metadata.json` (written next to the data) records: `batch`, `ingest_date`, `file_count`, `total_bytes`, `source_prefix`, `ingested_at`. The Glue job must skip it when reading the JSON data.

## Unit tests (`tests/test_ingest_handler.py`)
A small fake S3 client replaces AWS, so `pytest tests` takes 2 seconds and costs nothing. Covered: valid and invalid batch numbers (9 bad inputs), the date calculation, copying exactly the right files, not touching other batches, writing a correct manifest, failing on an empty batch, and the same result on a second run.
Result: **14 passed**.

## Deployment (`notebooks/ingest_lambda.ipynb`)
| Step | Command | Why |
|---|---|---|
| Package | Python `zipfile` | Lambda needs a zip with `handler.py` at its root |
| Does it exist? | `lambda get-function` | decides between create and update |
| Create | `lambda create-function --runtime python3.12 --handler handler.lambda_handler --role <arn> --zip-file fileb://... --timeout 60 --memory-size 256 --environment ...` | `fileb://` reads the zip as binary |
| Update (if it exists) | `lambda update-function-code`, then `update-function-configuration` | keeps the function in line with the notebook |
| Wait | `lambda wait function-updated-v2` / `function-active-v2` | a function cannot be changed while AWS is still updating it |
| Check | `lambda get-function-configuration` + asserts | runtime, handler, memory, timeout, role, bucket variable, state `Active` |

## Tests in AWS
| Test | Result |
|---|---|
| Invoke `{"batch": 1}` | `{"status": "OK", "batch": 1, "ingest_date": "2026-11-01", "file_count": 37}` |
| Bronze matches landing | `bronze/patient_records/ingest_date=2026-11-01/` holds the same 37 files with the same sizes (110.8 MB) plus the metadata file |
| Metadata file | `file_count` 37, `total_bytes` 110,770,899 = the sum of the landing file sizes |
| Run batch 1 again | identical result, same 38 objects (37 files + metadata file): no duplicates |
| `{"batch": 0}`, `{"batch": 11}`, `{"batch": "1"}`, `{}` | each fails with `ValueError` and a clear message |
| Logs | CloudWatch log group `/aws/lambda/pf-ingest` shows `ingest started` / `ingest finished` JSON lines |

**How a Lambda failure looks:** `lambda invoke` still returns HTTP `StatusCode: 200` when the function raised an error. The failure is in the `FunctionError` field (`Unhandled`) and the details are in the returned payload (`errorType`, `errorMessage`). The notebook checks `FunctionError`. In Phase 7, Step Functions treats a raised error as a failed step.

## Numbers worth knowing
- Billed duration of the first invocation: **10.1 s** for 37 files (the largest batch by file count), a little over a quarter of a second per copy. The 60 s timeout leaves a wide margin. Copies run one after another; if batches ever grew, copying in parallel threads would shorten it.
- Memory used: 93 MB of 256 MB.
- Cost: well inside the Lambda free tier.

## Design decisions
- **No "next batch" counter or state file.** The caller says which batch to run, so a re-run is simple and safe. (The plan's first draft used a state file; it was dropped to keep the first version simple.)
- **Fails on an empty batch** instead of succeeding quietly, so a wrong batch number or a missing upload is noticed immediately.
- **No cleanup of old files** in the target folder: batch contents do not change, so a rerun writes the same names. If a batch were ever re-delivered with different files, stale files would remain; the Phase 10 marker file with a file count would catch that.
- `batch_01` has 37 files because it also holds the hospital and practitioner files.

## Done when
- Batch 1 is in `bronze/` with a metadata file. ✅
- A rerun gives the same result. ✅
- Batch 0 and batch 11 fail with a clear error (now: anything outside 1-4). ✅

**State after this phase:** only batch 1 is in `bronze/`. Batches 2-4 are ingested when the pipeline runs them.
