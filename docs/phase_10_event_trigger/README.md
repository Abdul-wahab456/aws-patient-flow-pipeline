# Phase 10 — The event trigger: a finished delivery starts the pipeline

## Goal
Nobody should have to press a button. When a batch has been delivered to `landing/`, the pipeline starts by itself, runs every step in order, and stops with a clear error if the delivery is incomplete.

```
sender uploads the data files to  landing/batch_NN/        (any number of files, any order)
sender writes LAST                landing/batch_NN/_SUCCESS     {"files": 34}     "the delivery is complete"
        |  S3 sends "object created" to EventBridge
        v
EventBridge rule  pf-new-batch-rule      (only landing/batch_*/_SUCCESS in our bucket)
        |  uses role pf-trigger-role
        v
Step Functions  pf-batch-pipeline     input {"bucket": "...", "key": "landing/batch_NN/_SUCCESS"}
        v
Lambda: marker says 34 files, the batch holds 34?  yes -> ingest.  no -> fail ("incomplete batch")
        v
Glue job (silver) -> four merges (gold) -> done
```

## Why a marker file and not "any new file"?
A batch has 34–37 files. A trigger on "a file arrived" would start the pipeline once per file, and the first run could start while the delivery is still half uploaded. The marker file is written **last** and carries the number of files that were sent. This is the common convention for file deliveries (Hadoop and Spark use the same name, `_SUCCESS`).

## What this phase creates
| Item | Purpose |
|---|---|
| `lambda/ingest/handler.py` (changed) | Accepts `{"bucket", "key"}`; reads the file count from the marker; fails if the batch holds a different number; never copies the marker; accepts only our own bucket. `{"batch": N}` still works for manual starts. |
| `tests/test_ingest_handler.py` (more tests) | 37 tests, handler coverage 100% |
| `infra/policies/policy_trigger.json`, `permissions_trigger.template.json` | Trust and permissions of the new role |
| AWS role `pf-trigger-role` | Trusted by EventBridge; may only start the one state machine. It replaces `pf-scheduler-role` (deleted, it had never been used). |
| AWS: S3 bucket notifications | The bucket sends its events to EventBridge |
| AWS: EventBridge rule `pf-new-batch-rule` + one target | The trigger itself |
| `notebooks/event_trigger.ipynb` | Creates the trigger and tests it with real deliveries |
| `notebooks/iam_roles.ipynb` (updated) | Creates `pf-trigger-role`, removes `pf-scheduler-role`, 18 permission checks |
| `infra/aws_helpers.py` (fix) | A command that prints nothing now counts as "no data" |

## Concepts
- **Event-driven**: something happens (a file is created) and that event starts the work, instead of a timer or a person.
- **Amazon EventBridge**: AWS's event bus. S3 posts events to it (when the bucket asks for it); **rules** decide which events go where.
- **Event pattern**: a JSON filter. Ours: `source = aws.s3`, `detail-type = Object Created`, bucket name = ours, object key matches the wildcard `landing/batch_*/_SUCCESS`.
- **Target**: what the rule starts. Here the Step Functions state machine.
- **Input transformer**: reshapes the S3 event into the small input the workflow expects: `{"bucket": "<bucket>", "key": "<key>"}`.
- **Role for the target**: EventBridge needs permission to start the workflow. `pf-trigger-role` can start that one workflow and nothing else.

## Tests before AWS
37 Lambda tests with a fake S3. New ones: batch number taken from the marker key; keys that must be rejected (a data file, wrong folder, one-digit batch, nested folder, batch 5 or 0); marker content must be JSON like `{"files": 37}` (not JSON, `{}`, 0, text, `true` are rejected); the marker is neither counted nor copied; a count different from the announced one is rejected **before anything is copied**; a marker from another bucket is refused; `{"batch": N}` still works.

## Tests on AWS (`event_trigger.ipynb`)
| Test | Result |
|---|---|
| Event pattern against 7 sample events (AWS `test-event-pattern`) | Starts the pipeline for the markers of batch 2 and batch 10. Ignored: a data file in a batch, a `_SUCCESS` in `bronze/`, a file in silver, the marker in another bucket, the marker being **deleted** |
| **Release batch 2** (the only action: write the marker) | An execution started by itself, with input `{"bucket": ..., "key": "landing/batch_02/_SUCCESS"}`, and **SUCCEEDED**; output `{file_count 34, batch 2, ingest_date 2026-11-02}` |
| **Incomplete delivery**: batch 3's marker announces 999 files, 34 exist | `FAILED` at the first step: `incomplete batch 3: the marker announces 999 files but s3://…/landing/batch_03/ holds 34`. Steps entered: `Ingest`, `PipelineFailed` (Glue and the merges never ran) |
| **Delete a marker** | Nothing started (45 seconds observed) |

Problem met on the way: the first run stopped because S3 prints *nothing at all* for a bucket without notifications and the helper `aws_json` tried to read that as JSON. The helper now treats an empty answer as "no data".

## How a real delivery works
1. Upload the batch's data files to `landing/batch_NN/`.
2. Write `landing/batch_NN/_SUCCESS` with the number of files: `{"files": 34}`. **Last.**
3. Everything else is automatic. See the result in **Step Functions → `pf-batch-pipeline` → Executions** (executions started by EventBridge have a long random name).

In the notebook, `release_batch(n)` does steps 1–2's marker part for you (it counts the files itself).
To run a batch **again**, write its marker again: a new "created" event, a new run.

## Good to know
- **Two layers of checking.** The rule's wildcard `landing/batch_*/_SUCCESS` also matches deeper keys such as `landing/batch_02/sub/_SUCCESS`. Such a run starts and then fails clearly in the Lambda (it accepts only `landing/batch_NN/_SUCCESS`; tested). The rule is the coarse filter, the Lambda the strict one.
- **Release one batch at a time when you can.** Two at once work (the second waits at the Glue job, tested in Phase 7) but both write the same Iceberg tables.
- **Switch it off:** `aws events disable-rule --name pf-new-batch-rule` (and `enable-rule` to switch it on). While disabled, markers start nothing.
- **Cost:** S3 events to EventBridge and the rule are free at this volume. The pipeline run costs about $0.03 (Glue).
- **Not part of the GitHub deployment yet.** The Lambda is deployed by `infra/deploy.py` (so a push updates it), but the bucket setting, the rule and the target were created by this notebook. Phase 11 moves them into CloudFormation.

## Done when
- Releasing a batch runs the whole flow with no manual command. ✅
- An incomplete batch fails cleanly at the first step. ✅
- The trigger ignores everything except marker creation. ✅
