"""Small helpers shared by the Phase 3+ setup notebooks.

Import from a notebook with:

    import sys; sys.path.insert(0, str(ROOT / "infra"))   # ROOT = project root
    from aws_helpers import aws, whoami, SETTINGS, REGION, LAKE, RESULTS

Why a module: every setup notebook needs the same "run an AWS CLI command and
show it" function. Keeping one copy here means a fix is made once.
"""
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SETTINGS = json.loads((ROOT / "config" / "settings.json").read_text())
REGION = SETTINGS["region"]
LAKE = SETTINGS["lake_bucket"]
RESULTS = SETTINGS["athena_results_bucket"]

# A notebook can start with an older PATH than a new terminal, so fall back to
# the default install folder of the AWS CLI.
AWS_EXE = shutil.which("aws") or r"C:\Program Files\Amazon\AWSCLIV2\aws.exe"

# - AWS_DEFAULT_REGION is forced from settings.json, so a wrong default region
#   can never create resources in the wrong place.
# - AWS_PAGER is empty so the CLI never waits for a key press.
ENV = {**os.environ, "AWS_DEFAULT_REGION": REGION, "AWS_PAGER": ""}


class CliResult(subprocess.CompletedProcess):
    """Result of an AWS CLI call. Its repr is short, because Jupyter shows the repr of a cell's last value
    and the default one contains the whole command (which can include the account id)."""

    def __repr__(self):
        return f"<aws exit code {self.returncode}>"


_account_id = None


def account_id():
    """The 12-digit AWS account id (looked up once). Needed to build ARNs."""
    global _account_id
    if _account_id is None:
        result = subprocess.run([AWS_EXE, "sts", "get-caller-identity", "--query", "Account", "--output", "text"],
                                capture_output=True, text=True, env=ENV, check=True)
        _account_id = result.stdout.strip()
    return _account_id


def _mask(text):
    """Hide the account id in anything we print, so notebooks are safe to commit."""
    return text.replace(account_id(), "<ACCOUNT_ID>")


def aws(*args, check=True, show=True):
    """Run `aws <args>`, print the command and its output, return the result.

    check=True  -> raise an error if the command fails (default).
    check=False -> return quietly so the caller can decide
                   (used for "does this resource exist?" questions).
    show=False  -> print the command but not its output (for long answers).

    Commands run from the project root, so `file://infra/...` paths work.
    The account id is masked in everything printed.
    """
    print(_mask("$ aws " + " ".join(args)))
    result = subprocess.run([AWS_EXE, *args], capture_output=True, text=True, cwd=ROOT, env=ENV)
    if show and result.stdout.strip():
        print(_mask(result.stdout.strip()))
    if result.returncode != 0 and check:
        print(_mask(result.stderr.strip()))
        raise RuntimeError(f"aws {' '.join(args[:2])} failed with exit code {result.returncode}")
    return CliResult(result.args, result.returncode, result.stdout, result.stderr)


def aws_json(*args):
    """Run a command quietly and return its JSON output as Python data."""
    result = subprocess.run([AWS_EXE, *args], capture_output=True, text=True, cwd=ROOT, env=ENV)
    if result.returncode != 0:
        raise RuntimeError(f"aws {' '.join(args[:2])} failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def whoami():
    """Return the IAM identity (e.g. 'user/Ahmed') without exposing the account id."""
    return aws_json("sts", "get-caller-identity")["Arn"].split(":")[-1]


def run_athena(sql, workgroup=None, timeout=120):
    """Run one SQL statement in Athena, wait for it to finish and return the result.

    Athena is asynchronous: start-query-execution only *starts* the query and
    returns an id; we then poll get-query-execution until it reaches a final state.

    Returns a dict: query_id, scanned_bytes, header (column names), rows (list of lists).
    Raises RuntimeError if the query fails or is cancelled, TimeoutError if it takes too long.
    """
    workgroup = workgroup or SETTINGS["athena_workgroup"]
    query_id = aws_json("athena", "start-query-execution",
                        "--query-string", sql, "--work-group", workgroup)["QueryExecutionId"]

    deadline = time.time() + timeout
    while True:
        execution = aws_json("athena", "get-query-execution", "--query-execution-id", query_id)["QueryExecution"]
        state = execution["Status"]["State"]
        if state in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        if time.time() > deadline:
            raise TimeoutError(f"Athena query {query_id} still {state} after {timeout}s")
        time.sleep(1)

    if state != "SUCCEEDED":
        reason = execution["Status"].get("StateChangeReason", "no reason given")
        raise RuntimeError(f"Athena query {state}: {reason}")

    result = aws_json("athena", "get-query-results", "--query-execution-id", query_id)
    table = [[cell.get("VarCharValue") for cell in row["Data"]] for row in result["ResultSet"]["Rows"]]
    return {
        "query_id": query_id,
        "scanned_bytes": execution["Statistics"].get("DataScannedInBytes", 0),
        "header": table[0],
        "rows": table[1:],
    }


def run_glue_job(job_name, arguments, timeout=1200, poll=10):
    """Start a Glue job run, wait until it reaches a final state and return a summary.

    `arguments` are the job arguments, e.g. {"--ingest_date": "2026-11-01"}.
    Returns a dict: run_id, state, error (message or None), execution_seconds, dpu_seconds.
    Does not raise when the job fails; the caller decides (a test may expect a failure).
    """
    run_id = aws_json("glue", "start-job-run", "--job-name", job_name,
                      "--arguments", json.dumps(arguments))["JobRunId"]
    print(f"started {job_name} run {run_id}")

    deadline = time.time() + timeout
    last_state = None
    while True:
        run = aws_json("glue", "get-job-run", "--job-name", job_name, "--run-id", run_id)["JobRun"]
        state = run["JobRunState"]
        if state != last_state:
            print(f"  {time.strftime('%H:%M:%S')}  {state}")
            last_state = state
        if state in ("SUCCEEDED", "FAILED", "ERROR", "TIMEOUT", "STOPPED"):
            break
        if time.time() > deadline:
            raise TimeoutError(f"Glue run {run_id} still {state} after {timeout}s")
        time.sleep(poll)

    return {
        "run_id": run_id,
        "state": state,
        "error": run.get("ErrorMessage"),
        "execution_seconds": run.get("ExecutionTime"),
        "dpu_seconds": run.get("DPUSeconds"),
    }


def render_policy(filename):
    """Read a policy template from infra/policies/, fill in the __PLACEHOLDERS__ and return compact JSON text.

    The values come from config/settings.json and from the account id, so no account id or bucket
    name is stored in the template files. Fails if the JSON is invalid or a placeholder is left unfilled.
    """
    placeholders = {
        "__LAKE__": LAKE,
        "__RESULTS__": RESULTS,
        "__REGION__": REGION,
        "__ACCOUNT__": account_id(),
        "__LAMBDA__": SETTINGS["lambda_name"],
        "__GLUE_JOB__": SETTINGS["glue_job_name"],
        "__WORKGROUP__": SETTINGS["athena_workgroup"],
        "__STATE_MACHINE__": SETTINGS["state_machine_name"],
        "__DB_SILVER__": SETTINGS["glue_databases"]["silver"],
        "__DB_GOLD__": SETTINGS["glue_databases"]["gold"],
        "__REPOSITORY__": SETTINGS.get("github_repository", ""),
    }
    text = (ROOT / "infra" / "policies" / filename).read_text()
    for placeholder, value in placeholders.items():
        text = text.replace(placeholder, value)
    if "__" in text:
        raise ValueError(f"{filename}: unfilled placeholder left in the policy")
    return json.dumps(json.loads(text))
