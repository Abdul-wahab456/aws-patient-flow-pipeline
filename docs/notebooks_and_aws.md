# Notebooks and AWS — what runs where

**Question raised by the team:** will the code we write in notebooks work on AWS?

## Short answer
Notebooks run on the developer's laptop (VS Code, `.venv` kernel) and call AWS over the network. Nothing on AWS runs a notebook. The code that runs *on* AWS is stored in `.py` and `.sql` files.

| Code | Runs where | Written in |
|---|---|---|
| Exploration, data-quality and KPI notebooks | Laptop; calls AWS with `boto3` / `awswrangler` | `notebooks/*.ipynb` |
| Ingest function | AWS Lambda | `lambda/ingest/handler.py` |
| Bronze → silver transform | AWS Glue (Spark) | `glue_jobs/bronze_to_silver.py`, `glue_jobs/fhir_utils.py` |
| Gold tables, MERGE, views, data-quality checks | Amazon Athena | `sql/**/*.sql` |
| Orchestration | AWS Step Functions | `step_functions/*.asl.json` |

## How a notebook reaches AWS
The notebook's Python process reads the same credentials file as the AWS CLI (`~/.aws/credentials`, `~/.aws/config`). No keys appear in notebook code.

**Check performed (Phase 2, read-only):** from the project venv, `boto3` reported region `us-east-1` and identity `user/Ahmed`, and `awswrangler.catalog.databases()` ran successfully (empty list: no databases exist yet). So the notebook environment can already call AWS.

## Risks and how we cover them
| Risk | Why it matters | How we handle it |
|---|---|---|
| Logic works in a notebook but not in the Glue job | Glue runs Spark on its own Python version and libraries; a notebook uses pandas and our laptop's Python 3.12 | Transform logic goes in `fhir_utils.py` as plain Python functions with unit tests (`tests/`), and the Glue job is developed with PySpark, not pandas. In Phase 5, test in the official AWS Glue Docker image or a short Glue run before relying on it. Check the Python version of the chosen Glue version. |
| Notebook works only with admin rights | A real team uses least privilege | Notebooks use the project IAM user now; the Power BI read-only user (Phase 13) will prove read-only access works. |
| Hidden dependency on local files | Lambda/Glue cannot see our laptop | Deployed code reads only from S3 and gets its settings from arguments or `config`, never from `output/` or `data/`. |
| Cost from notebooks | Athena bills by data scanned | All queries run in workgroup `pf-wg` with a 1 GB per-query cutoff. |

## Checks still to do (when the resources exist)
- Phase 3: `SELECT 1` through Athena from Python (`awswrangler`) in the `pf-wg` workgroup.
- Phase 5: run `fhir_utils.py` tests locally, then run the real Glue job and compare row counts with the notebook's pandas prototype (341 patients, 10,876 encounters, 7,243 conditions for all batches together).
- Phase 8: notebooks query the gold views.

## Where to read more (search these titles in the official documentation)
- AWS Glue: "interactive sessions" and "developing and testing jobs locally with Docker"
- "AWS SDK for pandas" (awswrangler) — Athena section
- boto3 documentation — credentials and Athena client
