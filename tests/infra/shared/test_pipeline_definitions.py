"""Tests for infra/shared/settings.py and pipeline_definitions.py (settings, Glue job, Lambda, workflow). No AWS."""
import io
import json
import zipfile
from pathlib import Path

import pytest

from infra.shared import pipeline_definitions
from infra.shared.settings import ROOT, load_config


def test_config_comes_from_environment_variables():
    config = load_config(
        environ={"LAKE_BUCKET": "my-lake", "SILVER_DATABASE": "my_silver"},
        settings_path=Path("does-not-exist.json"),
    )
    assert config["lake_bucket"] == "my-lake" and config["silver_database"] == "my_silver"
    assert "number_of_batches" not in config            # there is no fixed number of batches
    assert config["glue_job"] == "pf-bronze-to-silver"      # default
    assert config["region"] == "us-east-1"


def test_config_falls_back_to_the_settings_file(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "region": "eu-west-1", "lake_bucket": "file-lake", "glue_databases": {"silver": "file_silver"},
    }))
    config = load_config(environ={"LAKE_BUCKET": "env-lake"}, settings_path=settings)

    assert config["lake_bucket"] == "env-lake"              # the environment wins
    assert config["silver_database"] == "file_silver"       # missing in the environment: taken from the file
    assert config["region"] == "eu-west-1"


def test_config_fails_clearly_when_a_required_setting_is_missing():
    with pytest.raises(ValueError, match="missing settings"):
        load_config(environ={}, settings_path=Path("does-not-exist.json"))


def test_glue_job_reads_the_scripts_of_the_given_version():
    settings = pipeline_definitions.glue_job_settings("arn:aws:iam::1:role/r", "lake", "silver_db", "abc123")

    assert settings["Command"]["ScriptLocation"] == "s3://lake/glue_scripts/abc123/bronze_to_silver.py"
    assert settings["DefaultArguments"]["--extra-py-files"] == "s3://lake/glue_scripts/abc123/fhir_utils.py"
    assert settings["DefaultArguments"]["--silver_database"] == "silver_db"
    # the cost guardrails of the plan
    assert (settings["GlueVersion"], settings["WorkerType"], settings["NumberOfWorkers"]) == ("5.0", "G.1X", 2)
    assert settings["Timeout"] == 10 and settings["MaxRetries"] == 0


def test_lambda_package_holds_handler_py_at_its_root():
    with zipfile.ZipFile(io.BytesIO(pipeline_definitions.lambda_package())) as archive:
        assert archive.namelist() == ["handler.py"]
        assert b"def lambda_handler" in archive.read("handler.py")


def test_lambda_settings_pass_the_pipeline_settings_as_environment_variables():
    config = {"lake_bucket": "lake", "first_ingest_date": "2026-11-01"}
    settings = pipeline_definitions.lambda_settings("arn:aws:iam::1:role/r", config)

    assert settings["Environment"]["Variables"] == {
        "LAKE_BUCKET": "lake", "FIRST_INGEST_DATE": "2026-11-01",
    }
    assert settings["Runtime"] == "python3.12" and settings["Handler"] == "handler.lambda_handler"


# ---- the Step Functions workflow -----------------------------------------------------------------------------
CONFIG = {"region": "us-east-1", "lambda_name": "pf-ingest", "glue_job": "pf-bronze-to-silver", "workgroup": "pf-wg"}


def workflow():
    return pipeline_definitions.state_machine_definition(CONFIG, "123456789012")


def test_config_has_defaults_for_the_workgroup_and_the_state_machine():
    config = load_config(environ={"LAKE_BUCKET": "b", "SILVER_DATABASE": "s"}, settings_path=Path("none.json"))
    assert config["workgroup"] == "pf-wg" and config["state_machine"] == "pf-batch-pipeline"


def test_workflow_has_no_unfilled_placeholders_and_names_the_right_resources():
    text = json.dumps(workflow())
    assert "__" not in text.replace("__init__", "")
    assert "arn:aws:lambda:us-east-1:123456789012:function:pf-ingest" in text
    assert '"JobName": "pf-bronze-to-silver"' in text and '"WorkGroup": "pf-wg"' in text


def test_workflow_steps_run_in_the_expected_order():
    states = workflow()["States"]
    order, current = [], workflow()["StartAt"]
    while states[current]["Type"] != "Choice":
        order.append(current)
        current = states[current]["Next"]
    assert order == ["Ingest", "BronzeToSilver", "PrepareDateParameter", "MergeDimPatient", "MergeDimFacility",
                     "MergeDimPractitioner", "MergeFactEncounter", "DataQualityChecks",
                     "ReadDataQualityResult"]
    # dimensions first, the fact last, the quality checks after the loads
    assert current == "EvaluateDataQuality"


def test_the_quality_decision_succeeds_on_ok_and_fails_with_the_names_of_the_failing_checks():
    states = workflow()["States"]
    decision = states["EvaluateDataQuality"]
    ok = {"Variable": "$.dq.failed_checks", "StringEquals": "OK", "Next": "PipelineSucceeded"}
    assert decision["Choices"] == [ok]
    assert decision["Default"] == "DataQualityFailed"
    assert states["DataQualityFailed"]["Next"] == "PipelineFailed"
    assert "failed_checks" in states["DataQualityFailed"]["Parameters"]["Cause.$"]


def test_the_quality_query_contains_every_check_file():
    query = workflow()["States"]["DataQualityChecks"]["Parameters"]["QueryString"]
    names = [path.stem for path in (ROOT / "sql" / "data_quality_checks").rglob("*.sql")]
    assert len(names) >= 14 and all(f"'{name}'" in query for name in names)


def test_every_next_points_to_a_real_state():
    definition = workflow()
    states = definition["States"]
    assert definition["StartAt"] in states
    for name, state in states.items():
        targets = [state["Next"]] if "Next" in state else []
        targets += [catch["Next"] for catch in state.get("Catch", [])]
        targets += [choice["Next"] for choice in state.get("Choices", [])]
        targets += [state["Default"]] if "Default" in state else []
        assert all(target in states for target in targets), name
        assert "Next" in state or state["Type"] in ("Succeed", "Fail", "Choice"), f"{name} leads nowhere"


def test_every_task_sends_errors_to_the_failure_state():
    for name, state in workflow()["States"].items():
        if state["Type"] == "Task":
            assert any(catch["Next"] == "PipelineFailed" and "States.ALL" in catch["ErrorEquals"]
                       for catch in state["Catch"]), f"{name} does not catch errors"


def test_each_athena_step_gets_exactly_as_many_date_parameters_as_it_has_question_marks():
    athena_states = {
        name: state for name, state in workflow()["States"].items()
        if state.get("Resource", "").endswith("startQueryExecution.sync")
    }
    assert len(athena_states) == 5       # four merges and the quality checks
    for name, state in athena_states.items():
        placeholders = state["Parameters"]["QueryString"].count("?")
        supplied = state["Parameters"]["ExecutionParameters.$"].count("$.sql.date")
        assert placeholders == supplied >= 1, f"{name}: {placeholders} ? but {supplied} parameters"


def test_the_merges_in_the_workflow_are_the_sql_files():
    states = workflow()["States"]
    expected_target = {
        "MergeDimPatient": "dim_patient", "MergeDimFacility": "dim_facility",
        "MergeDimPractitioner": "dim_practitioner", "MergeFactEncounter": "fact_encounter",
    }
    for state_name, table in expected_target.items():
        assert states[state_name]["Parameters"]["QueryString"].startswith(f"MERGE INTO pf_gold.{table} ")


def test_the_glue_step_waits_for_a_busy_job_instead_of_failing():
    retries = workflow()["States"]["BronzeToSilver"]["Retry"]
    assert any("Glue.ConcurrentRunsExceededException" in retry["ErrorEquals"] for retry in retries)
