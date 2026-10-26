"""Tests for the pure parts of infra/deploy.py (settings, job definition, Lambda package). No AWS needed."""
import io
import json
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "infra"))
import deploy  # noqa: E402


def test_config_comes_from_environment_variables():
    config = deploy.load_config(
        environ={"LAKE_BUCKET": "my-lake", "SILVER_DATABASE": "my_silver", "NUMBER_OF_BATCHES": "7"},
        settings_path=Path("does-not-exist.json"),
    )
    assert config["lake_bucket"] == "my-lake" and config["silver_database"] == "my_silver"
    assert config["number_of_batches"] == "7"
    assert config["glue_job"] == "pf-bronze-to-silver"      # default
    assert config["region"] == "us-east-1"


def test_config_falls_back_to_the_settings_file(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "region": "eu-west-1", "lake_bucket": "file-lake", "glue_databases": {"silver": "file_silver"},
        "number_of_batches": 4,
    }))
    config = deploy.load_config(environ={"LAKE_BUCKET": "env-lake"}, settings_path=settings)

    assert config["lake_bucket"] == "env-lake"              # the environment wins
    assert config["silver_database"] == "file_silver"       # missing in the environment: taken from the file
    assert config["region"] == "eu-west-1"
    assert config["number_of_batches"] == "4"


def test_config_fails_clearly_when_a_required_setting_is_missing():
    with pytest.raises(ValueError, match="missing settings"):
        deploy.load_config(environ={}, settings_path=Path("does-not-exist.json"))


def test_glue_job_reads_the_scripts_of_the_given_version():
    settings = deploy.glue_job_settings("arn:aws:iam::1:role/r", "lake", "silver_db", "abc123")

    assert settings["Command"]["ScriptLocation"] == "s3://lake/glue_scripts/abc123/bronze_to_silver.py"
    assert settings["DefaultArguments"]["--extra-py-files"] == "s3://lake/glue_scripts/abc123/fhir_utils.py"
    assert settings["DefaultArguments"]["--silver_database"] == "silver_db"
    # the cost guardrails of the plan
    assert (settings["GlueVersion"], settings["WorkerType"], settings["NumberOfWorkers"]) == ("5.0", "G.1X", 2)
    assert settings["Timeout"] == 10 and settings["MaxRetries"] == 0


def test_lambda_package_holds_handler_py_at_its_root():
    with zipfile.ZipFile(io.BytesIO(deploy.lambda_package())) as archive:
        assert archive.namelist() == ["handler.py"]
        assert b"def lambda_handler" in archive.read("handler.py")


def test_lambda_settings_pass_the_pipeline_settings_as_environment_variables():
    config = {"lake_bucket": "lake", "first_ingest_date": "2026-11-01", "number_of_batches": "4"}
    settings = deploy.lambda_settings("arn:aws:iam::1:role/r", config)

    assert settings["Environment"]["Variables"] == {
        "LAKE_BUCKET": "lake", "FIRST_INGEST_DATE": "2026-11-01", "NUMBER_OF_BATCHES": "4",
    }
    assert settings["Runtime"] == "python3.12" and settings["Handler"] == "handler.lambda_handler"
