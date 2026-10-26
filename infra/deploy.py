"""Deploy the pipeline code to AWS: the Glue scripts and Glue job, the ingest Lambda and the Step Functions workflow.

Run by GitHub Actions after every push to main (.github/workflows/deploy.yml). It can also be run by hand:

    python infra/deploy.py --version my-test

--version names this deployment. GitHub Actions passes the Git commit id, so each deployment keeps its
own copy of the scripts (s3://<lake>/glue_scripts/<version>/) and the Glue job always points at exactly
one commit. To roll back, deploy an older commit again.

Settings come from environment variables (used by GitHub Actions) or, when a variable is missing,
from config/settings.json (used on a laptop):
    LAKE_BUCKET, SILVER_DATABASE, GLUE_JOB_NAME, LAMBDA_NAME, FIRST_INGEST_DATE, NUMBER_OF_BATCHES, AWS_REGION,
    ATHENA_WORKGROUP, STATE_MACHINE_NAME
AWS credentials come from the normal AWS sources (GitHub Actions logs in with a short-lived role).
"""
import argparse
import io
import json
import os
import re
import zipfile
from pathlib import Path

import boto3
from sql_files import read_statements

ROOT = Path(__file__).resolve().parents[1]
GLUE_SCRIPTS = ("bronze_to_silver.py", "fhir_utils.py")
GLUE_ROLE = "pf-glue-role"
LAMBDA_ROLE = "pf-lambda-role"
STATE_MACHINE_ROLE = "pf-step-functions-role"


def load_config(environ=None, settings_path=None):
    """Settings from environment variables, falling back to config/settings.json for anything missing."""
    environ = os.environ if environ is None else environ
    settings_path = settings_path or ROOT / "config" / "settings.json"
    file_settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}

    def pick(variable, *file_keys, default=None):
        if environ.get(variable):
            return environ[variable]
        value = file_settings
        for key in file_keys:
            value = value.get(key, {}) if isinstance(value, dict) else {}
        return value if value not in ({}, None) else default

    config = {
        "region": pick("AWS_REGION", "region", default="us-east-1"),
        "lake_bucket": pick("LAKE_BUCKET", "lake_bucket"),
        "silver_database": pick("SILVER_DATABASE", "glue_databases", "silver"),
        "glue_job": pick("GLUE_JOB_NAME", "glue_job_name", default="pf-bronze-to-silver"),
        "lambda_name": pick("LAMBDA_NAME", "lambda_name", default="pf-ingest"),
        "first_ingest_date": pick("FIRST_INGEST_DATE", "first_ingest_date", default="2026-11-01"),
        "number_of_batches": str(pick("NUMBER_OF_BATCHES", "number_of_batches", default="4")),
        "workgroup": pick("ATHENA_WORKGROUP", "athena_workgroup", default="pf-wg"),
        "state_machine": pick("STATE_MACHINE_NAME", "state_machine_name", default="pf-batch-pipeline"),
    }
    missing = [name for name in ("lake_bucket", "silver_database") if not config[name]]
    if missing:
        raise ValueError(f"missing settings: {missing} (set the environment variables or config/settings.json)")
    return config


def glue_job_settings(role_arn, lake_bucket, silver_database, version):
    """The full definition of the Glue job. The scripts are read from the folder of this version."""
    scripts = f"s3://{lake_bucket}/glue_scripts/{version}"
    return {
        "Role": role_arn,
        "Command": {"Name": "glueetl", "ScriptLocation": f"{scripts}/bronze_to_silver.py", "PythonVersion": "3"},
        "GlueVersion": "5.0",
        "WorkerType": "G.1X",
        "NumberOfWorkers": 2,
        "Timeout": 10,
        "MaxRetries": 0,
        "DefaultArguments": {
            "--extra-py-files": f"{scripts}/fhir_utils.py",
            "--enable-glue-datacatalog": "true",
            "--lake_bucket": lake_bucket,
            "--silver_database": silver_database,
            "--TempDir": f"s3://{lake_bucket}/temp/",
            "--job-language": "python",
        },
    }


def lambda_package(root=ROOT):
    """The Lambda deployment package: a zip file holding handler.py at its root."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(root / "lambda" / "ingest" / "handler.py", arcname="handler.py")
    return buffer.getvalue()


def lambda_settings(role_arn, config):
    """Configuration of the ingest Lambda (same values the Phase 4 notebook sets)."""
    return {
        "Role": role_arn,
        "Runtime": "python3.12",
        "Handler": "handler.lambda_handler",
        "Timeout": 60,
        "MemorySize": 256,
        "Environment": {"Variables": {
            "LAKE_BUCKET": config["lake_bucket"],
            "FIRST_INGEST_DATE": config["first_ingest_date"],
            "NUMBER_OF_BATCHES": config["number_of_batches"],
        }},
    }


def deploy_glue_job(session, config, version):
    """Upload the scripts to glue_scripts/<version>/ and point the Glue job at them."""
    account = session.client("sts").get_caller_identity()["Account"]
    role_arn = f"arn:aws:iam::{account}:role/{GLUE_ROLE}"

    s3 = session.client("s3")
    for name in GLUE_SCRIPTS:
        key = f"glue_scripts/{version}/{name}"
        s3.upload_file(str(ROOT / "glue_jobs" / name), config["lake_bucket"], key)
        print(f"uploaded s3://{config['lake_bucket']}/{key}")

    glue = session.client("glue")
    settings = glue_job_settings(role_arn, config["lake_bucket"], config["silver_database"], version)
    try:
        glue.get_job(JobName=config["glue_job"])
    except glue.exceptions.EntityNotFoundException:
        glue.create_job(Name=config["glue_job"], Tags={"project": "patient-flow"}, **settings)
        print(f"created Glue job {config['glue_job']}")
    else:
        glue.update_job(JobName=config["glue_job"], JobUpdate=settings)
        print(f"updated Glue job {config['glue_job']}")
    print(f"Glue job now reads {settings['Command']['ScriptLocation']}")


def deploy_lambda(session, config):
    """Replace the code and the configuration of the ingest Lambda, waiting until AWS has finished."""
    account = session.client("sts").get_caller_identity()["Account"]
    role_arn = f"arn:aws:iam::{account}:role/{LAMBDA_ROLE}"

    client = session.client("lambda")
    name = config["lambda_name"]
    client.update_function_code(FunctionName=name, ZipFile=lambda_package())
    client.get_waiter("function_updated_v2").wait(FunctionName=name)    # code and config cannot change at once
    client.update_function_configuration(FunctionName=name, **lambda_settings(role_arn, config))
    client.get_waiter("function_updated_v2").wait(FunctionName=name)
    print(f"updated Lambda {name}")


def state_machine_definition(config, account, root=ROOT):
    """The Step Functions workflow, built from step_functions/pf_batch_pipeline.asl.json.

    The template holds placeholders: the names of the Lambda, the Glue job and the Athena workgroup, and
    "__SQL:<name>__" markers that are replaced by the text of sql/gold/merge/<name>.sql. The SQL therefore
    exists in one place only and the workflow always runs exactly the SQL files of the same commit.
    """
    template = json.loads((root / "step_functions" / "pf_batch_pipeline.asl.json").read_text(encoding="utf-8"))
    names = {
        "__LAMBDA_ARN__": f"arn:aws:lambda:{config['region']}:{account}:function:{config['lambda_name']}",
        "__GLUE_JOB__": config["glue_job"],
        "__WORKGROUP__": config["workgroup"],
    }

    def fill(node):
        if isinstance(node, dict):
            return {key: fill(value) for key, value in node.items()}
        if isinstance(node, list):
            return [fill(value) for value in node]
        if isinstance(node, str):
            sql_marker = re.fullmatch(r"__SQL:([a-z_]+)__", node)
            if sql_marker:
                return read_statements(root / "sql" / "gold" / "merge" / f"{sql_marker.group(1)}.sql", {})[0]
            for placeholder, value in names.items():
                node = node.replace(placeholder, value)
        return node

    return fill(template)


def deploy_state_machine(session, config):
    """Create or update the Step Functions state machine (with error logging to CloudWatch). Returns its ARN."""
    account = session.client("sts").get_caller_identity()["Account"]
    role_arn = f"arn:aws:iam::{account}:role/{STATE_MACHINE_ROLE}"
    name = config["state_machine"]

    # Workflow logs (level ERROR) go to a log group kept for 30 days
    log_group = f"/aws/vendedlogs/states/{name}"
    logs = session.client("logs")
    try:
        logs.create_log_group(logGroupName=log_group)
    except logs.exceptions.ResourceAlreadyExistsException:
        pass
    logs.put_retention_policy(logGroupName=log_group, retentionInDays=30)
    logging = {
        "level": "ERROR",
        "includeExecutionData": False,
        "destinations": [{"cloudWatchLogsLogGroup": {
            "logGroupArn": f"arn:aws:logs:{config['region']}:{account}:log-group:{log_group}:*"}}],
    }

    definition = json.dumps(state_machine_definition(config, account))
    sfn = session.client("stepfunctions")
    existing = [
        machine["stateMachineArn"]
        for page in sfn.get_paginator("list_state_machines").paginate()
        for machine in page["stateMachines"] if machine["name"] == name
    ]
    if existing:
        sfn.update_state_machine(stateMachineArn=existing[0], definition=definition, roleArn=role_arn,
                                 loggingConfiguration=logging)
        print(f"updated state machine {name}")
        return existing[0]
    arn = sfn.create_state_machine(
        name=name, definition=definition, roleArn=role_arn, type="STANDARD", loggingConfiguration=logging,
        tags=[{"key": "project", "value": "patient-flow"}],
    )["stateMachineArn"]
    print(f"created state machine {name}")
    return arn


def main():
    parser = argparse.ArgumentParser(description="Deploy the Glue job, the ingest Lambda and the state machine.")
    parser.add_argument("--version", required=True, help="name of this deployment, e.g. the Git commit id")
    args = parser.parse_args()

    config = load_config()
    session = boto3.Session(region_name=config["region"])
    print(f"deploying version {args.version} to {config['region']}")
    deploy_glue_job(session, config, args.version)
    deploy_lambda(session, config)
    deploy_state_machine(session, config)
    print("deployment finished")


if __name__ == "__main__":
    main()
