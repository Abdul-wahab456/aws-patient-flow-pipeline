"""Structure tests for the GitHub Actions workflows (.github/workflows). They cannot run here; their shape can."""
from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def load(name):
    workflow = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    workflow["on"] = workflow.pop(True)            # YAML reads the key `on` as the boolean True
    return workflow


def run_commands(workflow):
    return [step["run"] for job in workflow["jobs"].values() for step in job.get("steps", []) if "run" in step]


def test_ci_lints_the_code_and_the_templates_and_runs_the_tests():
    commands = " ".join(run_commands(load("ci.yml")))
    assert "ruff check" in commands and "cfn-lint infra/cloudformation/*.yaml" in commands and "pytest" in commands


def test_deploy_runs_ci_first_and_then_deploys_the_stacks_as_the_cloudformation_role():
    workflow = load("deploy.yml")
    assert workflow["jobs"]["deploy"]["needs"] == "checks"
    assert workflow["on"]["push"]["branches"] == ["main"]
    (command,) = [c for c in run_commands(workflow) if "stacks.py" in c]
    assert "--version" in command and "AWS_CLOUDFORMATION_ROLE_ARN" in command
    assert workflow["jobs"]["deploy"]["permissions"]["id-token"] == "write"     # login without stored keys


def test_no_workflow_stores_an_aws_key():
    for path in WORKFLOWS.glob("*.yml"):
        text = path.read_text(encoding="utf-8")
        assert "AWS_ACCESS_KEY_ID" not in text and "AWS_SECRET_ACCESS_KEY" not in text, path.name


def test_release_batch_is_started_by_hand_for_any_batch_number_and_never_two_at_once():
    workflow = load("release-batch.yml")
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    assert list(workflow["on"]) == ["workflow_dispatch"]
    assert inputs["batch"]["type"] == "string" and "options" not in inputs["batch"]     # a number box, not a fixed list
    assert workflow["concurrency"]["cancel-in-progress"] is False
    assert any("release_batch.py --batch" in command for command in run_commands(workflow))
    # the typed text reaches the script through an environment variable, never inside the shell command itself
    assert all("inputs.batch" not in command for command in run_commands(workflow))
    assert workflow["jobs"]["release"]["env"]["BATCH"] == "${{ inputs.batch }}"
