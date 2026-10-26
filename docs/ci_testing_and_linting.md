# Testing, linting and CI (GitHub Actions)

## The idea
Every time code is pushed to GitHub, a robot checks it automatically: **lint** (does the code have mistakes or style problems?) and **tests** (does it still do what it should?). If a check fails, GitHub shows a red cross next to the commit, so a mistake is noticed in minutes, not at deploy time. This is **CI** (Continuous Integration). **CD** (Continuous Deployment, putting the code on AWS automatically) comes in Phase 12, after the CloudFormation stacks exist.

## Tools
| Tool | What it does | Config |
|---|---|---|
| **pytest** | Finds and runs every test in `tests/` | `pyproject.toml` → `[tool.pytest.ini_options]` |
| **pytest-cov** | Shows which lines of the Lambda code the tests ran, and which they did not | same section (`--cov=lambda/ingest`) |
| **ruff** | Lint: unused or undefined names, likely bugs, import order, long lines. One fast tool instead of flake8 + isort | `pyproject.toml` → `[tool.ruff]` |

Ruff rules in use: `E`/`W` (pycodestyle), `F` (pyflakes), `I` (import order), `B` (bugbear, likely bugs); line length 120. Notebooks are linted too, with three rules relaxed (`E402`, `E501`, `I001`) because notebooks add the helper folder to `sys.path` before importing it and print long command lines.

## What the tests cover (30 tests)
| File | Tests | What |
|---|---|---|
| `tests/test_ingest_handler.py` | 17 | Batch-number validation (valid and 9 invalid inputs), the date calculation, copying exactly the right files, writing the metadata file, failing on an empty batch, running twice gives the same result, and the entry point `lambda_handler` (reads its settings from environment variables, rejects batch 5 when the maximum is 4, re-raises errors so the caller sees the failure). Uses a small fake S3 client. |
| `tests/test_json_files.py` | 13 | Every JSON file in `infra/` and `config/settings.example.json` is valid JSON. A typo in a policy file would otherwise only fail during deployment. |

Coverage of `lambda/ingest/handler.py`: **100%**.

**Rule for the project:** every new Python module gets tests in `tests/`. Phase 5 adds `tests/test_fhir_utils.py` for the flattening logic.

## Run the same checks on your laptop
From the project root, with the venv active:
```powershell
python -m ruff check .      # lint: "All checks passed!" or a list of problems
python -m pytest            # tests + coverage table
python -m ruff check . --fix   # (optional) lets ruff fix the simple problems itself
```
Run these before every commit. If they pass here, they pass on GitHub.

## The workflow file (`.github/workflows/ci.yml`)
| Part | What it does |
|---|---|
| `on: push, pull_request, workflow_dispatch` | Runs on every push to any branch, on every pull request, and by hand from the Actions tab |
| `permissions: contents: read` | The job may only read the code. It has **no AWS access and no secrets** |
| `runs-on: ubuntu-latest` | A fresh Linux machine provided by GitHub, thrown away after the run |
| `actions/checkout@v4` | Downloads the repository onto that machine |
| `actions/setup-python@v5` (3.12, `cache: pip`) | Installs Python 3.12 and caches downloaded packages for faster runs |
| `pip install -r requirements-ci.txt` | Installs only boto3, pytest, pytest-cov and ruff, **pinned to exact versions** so a new release of a tool cannot suddenly break the build |
| `python -m ruff check .` | The lint step |
| `python -m pytest` | The test step; fails if any test fails |

`requirements-ci.txt` is separate from `requirements.txt` on purpose: the full list includes jupyter, pandas and awswrangler, which the checks do not need and which would slow every run.

## How it was verified before the first push
GitHub Actions cannot run on a laptop, so the same two commands were run in a **brand-new empty environment containing only `requirements-ci.txt`** (no pandas, no jupyter, no awswrangler): lint passed and all 30 tests passed. This catches "works only because of something else installed here". The workflow file was also parsed to confirm the YAML is valid (one job, five steps, three triggers). What cannot be confirmed until the first push is the run on GitHub's machines themselves.

## Reading the result on GitHub
After a push: repository → **Actions** tab → the latest run. A green check means both steps passed; a red cross shows which step failed and prints its output (the lint messages or the failing test). The same status appears as a small icon next to each commit.

Private-repository note: GitHub gives free Actions minutes every month; this workflow takes about a minute per run.

## What comes next (Phase 12)
- `cfn-lint` step for the CloudFormation templates.
- The deploy workflow: GitHub logs in to AWS with a short-lived role (OIDC), so **no AWS keys are stored in GitHub**, then deploys the stacks. A deploy does not re-run data; a separate `release-batch` workflow does that on demand.
