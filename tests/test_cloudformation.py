"""Tests for the CloudFormation templates (infra/cloudformation) and infra/stacks.py. No AWS needed.

The IAM permissions exist twice: as JSON files (infra/policies/, used by the notebooks and tested with the AWS policy
simulator) and inside foundation.yaml. These tests make sure both copies say exactly the same.
"""
import json
import re
import sys
import zipfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "infra"))
import deploy  # noqa: E402
import stacks  # noqa: E402

VALUES = {
    "ProjectPrefix": "pf", "LakeBucketName": "lake-bucket", "ResultsBucketName": "results-bucket",
    "AWS::Region": "us-east-1", "AWS::AccountId": "123456789012",
}
PLACEHOLDERS = {
    "__LAKE__": "lake-bucket", "__RESULTS__": "results-bucket", "__REGION__": "us-east-1",
    "__ACCOUNT__": "123456789012",
    "__LAMBDA__": "pf-ingest", "__GLUE_JOB__": "pf-bronze-to-silver", "__WORKGROUP__": "pf-wg",
    "__STATE_MACHINE__": "pf-batch-pipeline", "__DB_SILVER__": "pf_silver", "__DB_GOLD__": "pf_gold",
}


class TemplateLoader(yaml.SafeLoader):
    """YAML loader that accepts CloudFormation's short forms (!Sub, !Ref, ...) and keeps them as {"Fn::Sub": ...}."""


def _intrinsic(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    return {"Ref" if suffix == "Ref" else f"Fn::{suffix}": value}


TemplateLoader.add_multi_constructor("!", _intrinsic)


def load_template(name):
    return yaml.load((ROOT / "infra" / "cloudformation" / name).read_text(encoding="utf-8"), Loader=TemplateLoader)


def fill(node):
    """Resolve {"Fn::Sub": "..."} and {"Ref": ...} with the test values, so the YAML can be compared with the JSON."""
    if isinstance(node, dict):
        if set(node) == {"Fn::Sub"}:
            return re.sub(r"\$\{([^}]+)\}", lambda match: VALUES[match.group(1)], node["Fn::Sub"])
        if set(node) == {"Ref"}:
            return VALUES[node["Ref"]]
        return {key: fill(value) for key, value in node.items()}
    if isinstance(node, list):
        return [fill(value) for value in node]
    return node


def json_policy(filename):
    text = (ROOT / "infra" / "policies" / filename).read_text(encoding="utf-8")
    for placeholder, value in PLACEHOLDERS.items():
        text = text.replace(placeholder, value)
    return json.loads(text)


def normalised(document):
    """A policy as comparable data: single values become lists and the lists are sorted."""
    def as_list(value):
        return sorted(value) if isinstance(value, list) else [value]

    statements = []
    for statement in document["Statement"]:
        statements.append({key: as_list(value) if key in ("Action", "Resource") else value
                           for key, value in statement.items()})
    return sorted(statements, key=lambda statement: statement.get("Sid", json.dumps(statement, sort_keys=True)))


foundation = load_template("foundation.yaml")
pipeline = load_template("pipeline.yaml")
RESOURCES = foundation["Resources"]

ROLES = {
    "LambdaRole": ("policy_lambda.json", "permissions_lambda.template.json"),
    "GlueRole": ("policy_glue.json", "permissions_glue.template.json"),
    "StepFunctionsRole": ("policy_step_functions.json", "permissions_step_functions.template.json"),
    "TriggerRole": ("policy_trigger.json", "permissions_trigger.template.json"),
}


@pytest.mark.parametrize("role", ROLES)
def test_the_role_permissions_in_the_template_are_the_tested_json_policies(role):
    trust_file, permissions_file = ROLES[role]
    properties = fill(RESOURCES[role]["Properties"])

    assert properties["AssumeRolePolicyDocument"] == json_policy(trust_file)
    (inline,) = properties["Policies"]
    assert normalised(inline["PolicyDocument"]) == normalised(json_policy(permissions_file))


def test_the_role_names_are_the_ones_the_rest_of_the_project_uses():
    names = {fill(RESOURCES[role]["Properties"])["RoleName"] for role in ROLES}
    assert names == {deploy.LAMBDA_ROLE, deploy.GLUE_ROLE, deploy.STATE_MACHINE_ROLE, "pf-trigger-role"}


def test_the_managed_policies_match_the_hand_built_roles():
    assert RESOURCES["LambdaRole"]["Properties"]["ManagedPolicyArns"] == [
        "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"]
    assert RESOURCES["GlueRole"]["Properties"]["ManagedPolicyArns"] == [
        "arn:aws:iam::aws:policy/service-role/AWSGlueServiceRole"]


@pytest.mark.parametrize("bucket", ["LakeBucket", "ResultsBucket"])
def test_buckets_are_private_encrypted_and_survive_a_stack_deletion(bucket):
    resource = RESOURCES[bucket]
    properties = resource["Properties"]
    assert resource["DeletionPolicy"] == "Retain" and resource["UpdateReplacePolicy"] == "Retain"
    block = properties["PublicAccessBlockConfiguration"]
    assert len(block) == 4 and all(block.values())
    rule = properties["BucketEncryption"]["ServerSideEncryptionConfiguration"][0]
    assert rule["ServerSideEncryptionByDefault"]["SSEAlgorithm"] == "AES256" and rule["BucketKeyEnabled"] is True


def test_the_lake_bucket_sends_events_to_eventbridge_and_the_results_expire_after_7_days():
    assert RESOURCES["LakeBucket"]["Properties"]["NotificationConfiguration"]["EventBridgeConfiguration"] == {
        "EventBridgeEnabled": True}
    (rule,) = RESOURCES["ResultsBucket"]["Properties"]["LifecycleConfiguration"]["Rules"]
    assert rule["ExpirationInDays"] == 7 and rule["Status"] == "Enabled"
    # the same settings as the JSON files the s3_buckets notebook applies
    notebook_rule = json.loads((ROOT / "infra" / "s3" / "lifecycle_athena_results.json").read_text())["Rules"][0]
    assert notebook_rule["Expiration"]["Days"] == rule["ExpirationInDays"]


def test_the_workgroup_enforces_its_settings_and_a_1_gb_scan_limit():
    configuration = fill(RESOURCES["Workgroup"]["Properties"])["WorkGroupConfiguration"]
    assert configuration["EnforceWorkGroupConfiguration"] is True
    assert configuration["BytesScannedCutoffPerQuery"] == 1024 ** 3
    assert configuration["ResultConfiguration"]["OutputLocation"] == "s3://results-bucket/"
    assert configuration["EngineVersion"]["SelectedEngineVersion"] == "Athena engine version 3"


def test_the_catalog_databases_are_the_ones_the_sql_files_name():
    assert RESOURCES["SilverDatabase"]["Properties"]["DatabaseInput"]["Name"] == "pf_silver"
    assert RESOURCES["GoldDatabase"]["Properties"]["DatabaseInput"]["Name"] == "pf_gold"
    sql = "".join(path.read_text(encoding="utf-8") for path in (ROOT / "sql" / "gold" / "merge").glob("*.sql"))
    assert "pf_gold." in sql and "pf_silver." in sql


def test_every_role_and_bucket_is_exported_for_the_pipeline_stack():
    exports = {name for output in foundation["Outputs"].values() for name in [output["Export"]["Name"]["Fn::Sub"]]}
    imported = set(re.findall(r"\$\{FoundationStackName\}-(\w+)", json.dumps(pipeline)))
    assert imported and {f"${{AWS::StackName}}-{name}" for name in imported} <= exports


RESOURCES_PIPELINE = pipeline["Resources"]


def test_the_lambda_is_configured_like_deploy_lambda_does_it():
    properties = RESOURCES_PIPELINE["IngestFunction"]["Properties"]
    expected = deploy.lambda_settings("role", {"lake_bucket": "x", "first_ingest_date": "d"})
    for key in ("Runtime", "Handler", "Timeout", "MemorySize"):
        assert properties[key] == expected[key]
    assert set(properties["Environment"]["Variables"]) == set(expected["Environment"]["Variables"])


def test_there_is_no_fixed_number_of_batches_anywhere_in_the_pipeline_stack():
    assert "NumberOfBatches" not in pipeline["Parameters"]
    assert "NUMBER_OF_BATCHES" not in json.dumps(pipeline)
    assert "NUMBER_OF_BATCHES" not in RESOURCES_PIPELINE["IngestFunction"]["Properties"]["Environment"]["Variables"]


def test_the_glue_job_is_configured_like_deploy_glue_job_does_it():
    properties = RESOURCES_PIPELINE["BronzeToSilverJob"]["Properties"]
    expected = deploy.glue_job_settings("role", "lake", "pf_silver", "v1")
    for key in ("GlueVersion", "WorkerType", "NumberOfWorkers", "Timeout", "MaxRetries"):
        assert properties[key] == expected[key]
    assert properties["ExecutionProperty"]["MaxConcurrentRuns"] == 1
    assert set(properties["DefaultArguments"]) == set(expected["DefaultArguments"])
    assert properties["DefaultArguments"]["--silver_database"] == "pf_silver"


def test_the_template_reads_the_code_from_the_keys_that_stacks_py_uploads(tmp_path):
    config = {**{"project_prefix": "pf", "region": "us-east-1", "lake_bucket": "lake", "results_bucket": "r",
                 "first_ingest_date": "2026-11-01", "silver_database": "pf_silver"}}
    built = stacks.build_artifacts(config, "123456789012", "v1", tmp_path)
    text = json.dumps(pipeline)
    for name in built:
        assert f"artifacts/${{DeploymentVersion}}/{name}" in text
    for name in deploy.GLUE_SCRIPTS:
        assert f"glue_scripts/${{DeploymentVersion}}/{name}" in text


def test_the_built_workflow_uses_the_resource_names_of_the_template(tmp_path):
    config = {"project_prefix": "pfx", "region": "us-east-1", "lake_bucket": "lake", "results_bucket": "r",
              "first_ingest_date": "2026-11-01", "silver_database": "pf_silver",
              "lambda_name": "ignored", "glue_job": "ignored", "workgroup": "ignored", "state_machine": "ignored"}
    built = stacks.build_artifacts(config, "123456789012", "v1", tmp_path)
    definition = built["state_machine.json"].read_text(encoding="utf-8")
    assert "function:pfx-ingest" in definition and '"JobName": "pfx-bronze-to-silver"' in definition
    assert '"WorkGroup": "pfx-wg"' in definition and "ignored" not in definition
    assert "__" not in definition.replace("__init__", "")
    with zipfile.ZipFile(built["ingest.zip"]) as archive:
        assert archive.namelist() == ["handler.py"]


def test_the_event_rule_is_the_one_built_and_tested_in_the_notebook():
    properties = RESOURCES_PIPELINE["NewBatchRule"]["Properties"]
    pattern = properties["EventPattern"]
    assert pattern["source"] == ["aws.s3"] and pattern["detail-type"] == ["Object Created"]
    assert pattern["detail"]["object"]["key"] == [{"wildcard": "landing/batch_*/_SUCCESS"}]
    (target,) = properties["Targets"]
    paths = target["InputTransformer"]["InputPathsMap"]
    assert paths == {"bucket": "$.detail.bucket.name", "key": "$.detail.object.key"}
    assert json.loads(target["InputTransformer"]["InputTemplate"]) == {"bucket": "<bucket>", "key": "<key>"}
    assert properties["State"] == "ENABLED"


def test_the_workflow_logs_errors_only_and_without_data():
    logging = RESOURCES_PIPELINE["Workflow"]["Properties"]["LoggingConfiguration"]
    assert logging["Level"] == "ERROR" and logging["IncludeExecutionData"] is False


def test_the_deploy_command_creates_or_updates_a_named_stack_with_its_parameters():
    command = stacks.deploy_command("pf-pipeline", Path("t.yaml"), {"A": "1", "B": "x y"}, "us-east-1", "arn:role")
    assert command[1:3] == ["cloudformation", "deploy"]
    assert command[command.index("--stack-name") + 1] == "pf-pipeline"
    assert command[command.index("--capabilities") + 1] == "CAPABILITY_NAMED_IAM"
    assert "--no-fail-on-empty-changeset" in command
    start = command.index("--parameter-overrides") + 1
    assert command[start:start + 2] == ["A=1", "B=x y"]
    assert command[command.index("--role-arn") + 1] == "arn:role"
    assert "--role-arn" not in stacks.deploy_command("s", Path("t.yaml"), {}, "us-east-1")


def test_the_stack_parameters_cover_every_parameter_without_a_default():
    config = {"project_prefix": "pf", "lake_bucket": "l", "results_bucket": "r", "first_ingest_date": "2026-11-01"}
    for template, supplied in ((foundation, stacks.foundation_parameters(config)),
                               (pipeline, stacks.pipeline_parameters(config, "v1"))):
        required = {name for name, spec in template["Parameters"].items() if "Default" not in spec}
        assert required <= set(supplied) <= set(template["Parameters"])


# ---- github_oidc.yaml: who may deploy, and what the CloudFormation role may create ---------------------------------
cicd = load_template("github_oidc.yaml")
DEPLOY_ROLE = cicd["Resources"]["DeployRole"]["Properties"]
SERVICE_ROLE = cicd["Resources"]["CloudFormationRole"]["Properties"]


def statements(properties):
    (policy,) = properties["Policies"]
    return {statement["Sid"]: statement for statement in policy["PolicyDocument"]["Statement"]}


def actions(statement):
    return [statement["Action"]] if isinstance(statement["Action"], str) else statement["Action"]


def test_github_may_assume_the_deploy_role_only_with_the_subject_given_as_a_parameter():
    (trust,) = DEPLOY_ROLE["AssumeRolePolicyDocument"]["Statement"]
    assert trust["Action"] == "sts:AssumeRoleWithWebIdentity"
    assert trust["Condition"]["StringEquals"] == {
        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
        "token.actions.githubusercontent.com:sub": {"Ref": "GitHubSubject"},
    }


def test_the_deploy_role_cannot_create_resources_itself_it_can_only_hand_over_to_cloudformation():
    allowed = statements(DEPLOY_ROLE)
    every_action = [action for statement in allowed.values() for action in actions(statement)]
    assert not any(action.split(":")[0] in ("iam", "lambda", "glue", "athena", "events") for action in every_action
                   if action != "iam:PassRole")
    assert not any(action.endswith("*") for action in every_action)
    hand_over = allowed["HandTheWorkToTheCloudFormationRoleOnly"]
    assert hand_over["Resource"] == {"Fn::GetAtt": "CloudFormationRole.Arn"}
    assert hand_over["Condition"]["StringEquals"]["iam:PassedToService"] == "cloudformation.amazonaws.com"


def test_the_deploy_role_may_only_write_the_code_and_the_batch_markers_to_the_lake():
    allowed = statements(DEPLOY_ROLE)
    written = allowed["UploadTheCode"]["Resource"] + [allowed["ReleaseABatch"]["Resource"]]
    assert [fill(item) for item in written] == [
        "arn:aws:s3:::lake-bucket/glue_scripts/*", "arn:aws:s3:::lake-bucket/artifacts/*",
        "arn:aws:s3:::lake-bucket/landing/*/_SUCCESS"]


def test_the_cloudformation_role_works_only_on_resources_with_the_project_names():
    for sid, statement in statements(SERVICE_ROLE).items():
        resources = statement["Resource"] if isinstance(statement["Resource"], list) else [statement["Resource"]]
        if resources == ["*"]:
            assert sid == "LogsThatNameNoResource", sid          # log delivery actions cannot name a resource
            continue
        for resource in resources:
            text = fill(resource) if isinstance(resource, dict) else resource
            names = ("pf-", "pf_", "lake-bucket", "results-bucket", ":catalog")    # the Glue catalog has no name
            assert any(name in text for name in names), (sid, text)


def test_the_cloudformation_role_can_attach_only_the_two_managed_policies_and_pass_only_the_four_roles():
    allowed = statements(SERVICE_ROLE)
    attach = allowed["AttachOnlyTheTwoAwsManagedPolicies"]["Condition"]["ArnEquals"]["iam:PolicyARN"]
    assert sorted(attach) == sorted(
        ["arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
         "arn:aws:iam::aws:policy/service-role/AWSGlueServiceRole"])
    passed = [fill(resource) for resource in allowed["GiveTheRolesToTheServices"]["Resource"]]
    assert sorted(passed) == sorted(f"arn:aws:iam::123456789012:role/pf-{name}-role"
                                    for name in ("lambda", "glue", "step-functions", "trigger"))
    for statement in allowed.values():
        assert "iam:*" not in actions(statement) and "iam:CreateUser" not in actions(statement)


def test_the_github_provider_is_created_only_when_the_account_has_none():
    assert cicd["Conditions"]["CreateProvider"] == {"Fn::Equals": [{"Ref": "OidcProviderArn"}, ""]}
    assert cicd["Resources"]["GitHubProvider"]["Condition"] == "CreateProvider"
