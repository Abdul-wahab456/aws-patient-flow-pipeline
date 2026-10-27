"""The project's settings: where the project root is, and the names and values every script needs.

Settings come from environment variables (used by GitHub Actions) or, when a variable is missing,
from config/settings.json (used on a laptop):
    LAKE_BUCKET, SILVER_DATABASE, GLUE_JOB_NAME, LAMBDA_NAME, FIRST_INGEST_DATE, AWS_REGION,
    ATHENA_WORKGROUP, STATE_MACHINE_NAME, RESULTS_BUCKET, PROJECT_PREFIX
"""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]     # this file is infra/shared/settings.py: two folders up is the project


def load_config(environ=None, settings_path=None):
    """Settings from environment variables, falling back to config/settings.json for anything missing."""
    environ = os.environ if environ is None else environ
    settings_path = settings_path or ROOT / "config" / "settings.json"
    file_settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}

    def pick(variable, *file_keys, default=None):
        """One setting: the environment variable if it is set, else the key in settings.json, else the default."""
        if environ.get(variable):
            return environ[variable]
        value = file_settings
        for key in file_keys:          # e.g. ("glue_databases", "silver") reads settings["glue_databases"]["silver"]
            value = value.get(key, {}) if isinstance(value, dict) else {}
        return value if value not in ({}, None) else default

    config = {
        "region": pick("AWS_REGION", "region", default="us-east-1"),
        "lake_bucket": pick("LAKE_BUCKET", "lake_bucket"),
        "silver_database": pick("SILVER_DATABASE", "glue_databases", "silver"),
        "glue_job": pick("GLUE_JOB_NAME", "glue_job_name", default="pf-bronze-to-silver"),
        "lambda_name": pick("LAMBDA_NAME", "lambda_name", default="pf-ingest"),
        "first_ingest_date": pick("FIRST_INGEST_DATE", "first_ingest_date", default="2026-11-01"),
        "workgroup": pick("ATHENA_WORKGROUP", "athena_workgroup", default="pf-wg"),
        "state_machine": pick("STATE_MACHINE_NAME", "state_machine_name", default="pf-batch-pipeline"),
        # only the CloudFormation stacks (infra/github_actions/deploy_stacks.py) need these two
        "results_bucket": pick("RESULTS_BUCKET", "athena_results_bucket"),
        "project_prefix": pick("PROJECT_PREFIX", "project_prefix", default="pf"),
    }
    # Only these two have no sensible default; everything else can fall back to the project's names
    missing = [name for name in ("lake_bucket", "silver_database") if not config[name]]
    if missing:
        raise ValueError(f"missing settings: {missing} (set the environment variables or config/settings.json)")
    return config
