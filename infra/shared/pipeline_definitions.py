"""The definitions of the pipeline pieces: the Glue job, the ingest Lambda and the Step Functions workflow.

infra/github_actions/deploy_stacks.py (run by GitHub Actions after every push to main) uses these to build and
upload the code, and CloudFormation (infra/cloudformation/) creates the resources from them. Nothing here talks to
AWS, so every function can be tested on a laptop.
"""
import io
import json
import re
import zipfile

from infra.shared.settings import ROOT
from infra.shared.sql_files import failed_checks_query, read_checks, read_statements

# The two files of the Glue job: the script Glue runs, and the toolbox it imports
GLUE_SCRIPTS = ("bronze_to_silver.py", "fhir_utils.py")


def glue_job_settings(role_arn, lake_bucket, silver_database, version):
    """The full definition of the Glue job. The scripts are read from the folder of this version."""
    scripts = f"s3://{lake_bucket}/glue_scripts/{version}"
    return {
        "Role": role_arn,
        "Command": {"Name": "glueetl", "ScriptLocation": f"{scripts}/bronze_to_silver.py", "PythonVersion": "3"},
        "GlueVersion": "5.0",
        # Cost guardrails: 2 small workers, stop after 10 minutes, never retry by itself (the workflow decides)
        "WorkerType": "G.1X",
        "NumberOfWorkers": 2,
        "Timeout": 10,
        "MaxRetries": 0,
        "DefaultArguments": {
            "--extra-py-files": f"{scripts}/fhir_utils.py",       # makes `import fhir_utils` work in the script
            # lets Spark use the Glue Data Catalog as its Hive metastore (so spark.sql can create tables)
            "--enable-glue-datacatalog": "true",
            "--lake_bucket": lake_bucket,
            "--silver_database": silver_database,
            "--TempDir": f"s3://{lake_bucket}/temp/",              # Glue's scratch space
            "--job-language": "python",
        },
    }


def lambda_package(root=ROOT):
    """The Lambda deployment package: a zip file holding handler.py at its root."""
    # Lambda needs a zip, and handler.py must sit at its root because the handler setting says "handler.lambda_handler"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(root / "lambda" / "ingest" / "handler.py", arcname="handler.py")
    return buffer.getvalue()


def lambda_settings(role_arn, config):
    """Configuration of the ingest Lambda (same values the ingest_lambda notebook sets)."""
    return {
        "Role": role_arn,
        "Runtime": "python3.12",
        "Handler": "handler.lambda_handler",
        # A batch is 34-37 server-side copies, a few seconds; 60 s leaves room, 256 MB is plenty
        "Timeout": 60,
        "MemorySize": 256,
        "Environment": {"Variables": {
            "LAKE_BUCKET": config["lake_bucket"],
            "FIRST_INGEST_DATE": config["first_ingest_date"],
        }},
    }


def state_machine_definition(config, account, root=ROOT):
    """The Step Functions workflow, built from step_functions/pf_batch_pipeline.asl.json.

    The template holds placeholders: the names of the Lambda, the Glue job and the Athena workgroup,
    "__SQL:<name>__" markers that are replaced by the text of sql/gold/merge_batch/<name>.sql, and "__SQL_DQ__", which
    becomes the one query that runs every check of sql/data_quality_checks. The SQL therefore exists in one place
    only and the workflow always runs exactly the SQL files of the same commit.
    """
    template = json.loads((root / "step_functions" / "pf_batch_pipeline.asl.json").read_text(encoding="utf-8"))
    names = {
        "__LAMBDA_ARN__": f"arn:aws:lambda:{config['region']}:{account}:function:{config['lambda_name']}",
        "__GLUE_JOB__": config["glue_job"],
        "__WORKGROUP__": config["workgroup"],
    }

    def fill(node):
        # Walk the whole JSON and replace placeholders inside every string, wherever they are
        if isinstance(node, dict):
            return {key: fill(value) for key, value in node.items()}
        if isinstance(node, list):
            return [fill(value) for value in node]
        if isinstance(node, str):
            if node == "__SQL_DQ__":      # the whole string is the marker: it becomes the combined quality query
                return failed_checks_query(list(read_checks(root / "sql" / "data_quality_checks").values()))
            # "__SQL:merge_dim_patient__" becomes the text of sql/gold/merge_batch/merge_dim_patient.sql
            sql_marker = re.fullmatch(r"__SQL:([a-z_]+)__", node)
            if sql_marker:
                return read_statements(root / "sql" / "gold" / "merge_batch" / f"{sql_marker.group(1)}.sql", {})[0]
            for placeholder, value in names.items():
                node = node.replace(placeholder, value)
        return node

    return fill(template)
