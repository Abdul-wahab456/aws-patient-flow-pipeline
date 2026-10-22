# Phase 1 — Repository Scaffolding

## Goal

Give the project a clean, professional structure, an isolated Python environment, and a Git repository pushed to GitHub, so everything we build later has a place to live and is portfolio-ready from day one. No AWS resources are created in this phase.

## Concepts

- **Repository structure**: one folder per kind of thing (infrastructure scripts, SQL, Spark code, notebooks, tests, docs). A teammate can find anything without asking.
- **`.gitkeep`**: Git does not track empty folders. An empty file named `.gitkeep` inside a folder lets us commit the folder before it has real content. It is deleted/ignored in practice once real files arrive.
- **Virtual environment (`.venv`)**: a private copy of Python for this project, so its packages don't mix with other projects.
- **`requirements.txt`**: the list of packages the project needs, so anyone can recreate the environment with one command.
- **Settings template pattern**: `config/settings.example.json` (committed, safe values) is copied to `config/settings.json` (gitignored, your real values).

## Tools found on this laptop (read-only checks)

| Tool              | Found                                                            | Note                                                           |
| ----------------- | ---------------------------------------------------------------- | -------------------------------------------------------------- |
| Python            | `python` = 3.13.0; **3.12 also installed** (found with `py -0p`) | The plan requires 3.12, so we create the venv with `py -3.12`. |
| Git               | 2.46.0                                                           | Identity already set (`user.name`, `user.email`).              |
| GitHub CLI (`gh`) | Not installed                                                    | Not needed; we create the repo on github.com.                  |

## What was created

### Folders

| Folder                                          | What it will hold                                                               | Phase  |
| ----------------------------------------------- | ------------------------------------------------------------------------------- | ------ |
| `config/`                                       | project names/settings                                                          | 1      |
| `data_generation/`                              | Synthea instructions and the batch-splitting script                             | 2      |
| `infra/` (+ `infra/policies/`, `infra/budget/`) | PowerShell + AWS CLI scripts, IAM policy JSON, budget files                     | 0, 3–7 |
| `lambda/ingest/`                                | the ingestion Lambda function (bronze)                                          | 4      |
| `glue_jobs/`                                    | Spark job and helper functions (bronze to silver)                               | 5      |
| `sql/gold/ddl`, `merge`, `views`; `sql/dq/`     | Athena SQL: table definitions, MERGE statements, KPI views, data-quality checks | 6, 8   |
| `step_functions/`                               | the workflow definition                                                         | 7      |
| `notebooks/`                                    | explanation notebooks, one per topic                                            | 2+     |
| `tests/`                                        | automated tests                                                                 | 5      |
| `docs/`                                         | one README per phase (this file)                                                | all    |

Each empty folder contains a `.gitkeep` file (see Concepts).

### Files

| File                            . | Why it exists                                                    | What is inside                                                                                                                                                                                                        |
| --------------------------------- | ---------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `requirements.txt`                | Reproducible Python environment                                  | `boto3` (AWS SDK for Python), `awswrangler` (pandas + Athena/S3), `pandas`, `matplotlib` (charts), `jupyter` (notebooks), `pytest` (tests)                                                                            |
| `config/settings. example.json`   | Single place for names used by scripts, so nothing is hard-coded | region `us-east-1`, bucket names, Glue databases, Athena workgroup, Lambda/Glue/Step Functions names, first ingest date, number of batches. `<suffix>` in the bucket name is replaced with a unique value in Phase 3. |
| `README.md`                       | The GitHub front page                                            | Short project summary; the full portfolio README is written in Phase 9.                                                                                                                                               |
| `.gitignore`                      | Created in Phase 0                                               | Keeps secrets, data, and personal files out of Git.                                                                                                                                                                   |

## Steps you run

### Step 1 — Create and activate the virtual environment (done)

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python --version
```

- `py -3.12` = Windows Python launcher, forced to Python 3.12 (the default `python` is 3.13).
- `-m venv .venv` = run Python's built-in venv tool and create the folder `.venv`.
- `Activate.ps1` = switches this terminal to use the venv's Python and pip. The prompt then starts with `(.venv)`.
- **Result:** `Python 3.12.10`.

### Step 2 — Install the packages (done)

```powershell
python -m pip install --upgrade pip
pip install -r requirements.txt
pip list --format=columns | Select-String "boto3|awswrangler|pandas|pytest|jupyter"
```

- `python -m pip install --upgrade pip` = updates pip itself. Using `python -m pip` guarantees we update the pip belonging to the active venv.
- `pip install -r requirements.txt` = installs every package listed in the file (`-r` = read the list from a file).
- `pip list` + `Select-String` = list installed packages and keep only the lines we care about.
- **Result:** boto3 1.43.117, awswrangler 3.17.1, pandas 3.1.0, pytest 9.1.1, jupyter 1.1.1 / jupyterlab 4.6.4.
- Versions are not pinned in `requirements.txt`, so a future install may get newer versions. `[EXTRA]` Later we can run `pip freeze > requirements-lock.txt` to record exact versions.

### Step 3 — Create the local Git repository (done)

```powershell
git init -b main
git status --short
git check-ignore -v infra/budget/notifications.json
```

- `git init -b main` = creates the hidden `.git` folder (the project history) and names the first branch `main`.
- `git status --short` = compact list of files Git sees. `.venv/` and `.env` did not appear: `.gitignore` works.
- `git check-ignore -v <file>` = shows which `.gitignore` rule blocks a file. Output `.gitignore:6:infra/budget/notifications.json` proves the email file is protected.

### Step 4 — First commit (done)

```powershell
git add .
git status --short
git commit -m "Phase 0-1: project scaffolding, docs, budget config and gitignore"
git log --oneline
```

- `git add .` = stage everything that is not ignored ("include in the next snapshot").
- `git commit -m` = save the snapshot with a message. A commit is a permanent, named point in history.
- `git log --oneline` = one line per commit.
- **Result:** commit `ab46dbb`, 19 files. We verified that no secret file is tracked (`notifications.json`, `.env`, `.venv`, `settings.json`) and that the account ID and key fragments appear in no committed file.
- **Privacy note:** the commit author email (`git config --global user.email`) is public on GitHub once pushed. GitHub offers a private "noreply" address in Settings → Emails. We decided to keep the current email.

### Step 5 — Push to GitHub (done)

Repo created on github.com (name `aws-patient-flow-pipeline`, **private**, created empty: no README/.gitignore/license ticked so it would not conflict with ours).

```powershell
git remote add origin https://github.com/Abdul-wahab456/aws-patient-flow-pipeline.git
git remote -v
git push -u origin main
```

- `git remote add origin <URL>` = stores the GitHub address under the name `origin` (the conventional name for the main remote copy).
- `git remote -v` = lists remotes (fetch and push URLs).
- `git push -u origin main` = uploads `main`. `-u` links local `main` to `origin/main`, so later a plain `git push` is enough.
- **Verification:** local `HEAD` and `origin/main` are the same commit (`ab46dbb`); no secret file exists on the remote.

## Done when

- Repo pushed. ✅
- `config/settings.json` and `infra/budget/notifications.json` are not tracked. ✅

## Everyday Git routine (used at the end of each phase)

```powershell
git status --short
git add .
git commit -m "<what changed>"
git push
```
