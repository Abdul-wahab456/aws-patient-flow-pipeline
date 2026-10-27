"""Tests for infra/teardown.py with fake AWS clients. Nothing is deleted anywhere."""
import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "infra"))
import teardown  # noqa: E402

CONFIG = {"project_prefix": "pf", "lake_bucket": "lake", "results_bucket": "results"}


def error(code):
    return ClientError({"Error": {"Code": code, "Message": "x"}}, "Operation")


class Recorder:
    """A fake client: every call is recorded; names listed in `fail` raise the given error code."""

    def __init__(self, log, name, fail=None, pages=None):
        self.log, self.name, self.fail, self.pages = log, name, fail or {}, pages or {}

    def __getattr__(self, method):
        def call(**kwargs):
            self.log.append((self.name, method, kwargs))
            if method in self.fail:
                raise error(self.fail[method])
            return self.pages.get(method, {})
        return call

    def get_paginator(self, method):
        recorder = self

        class Paginator:
            def paginate(self, **kwargs):
                recorder.log.append((recorder.name, method, kwargs))
                return recorder.pages.get(method, [{}])
        return Paginator()


class FakeSession:
    def __init__(self, **clients):
        self.log, self.clients = [], clients

    def client(self, name):
        spec = self.clients.get(name, {})
        return Recorder(self.log, name, **spec)


def test_the_hand_built_resources_are_deleted_in_a_safe_order():
    plan = teardown.hand_built_resources(CONFIG)
    kinds = [kind for kind, _ in plan]
    assert kinds[0] == "rule" and kinds[-2:] == ["bucket", "bucket"]                 # trigger first, data last
    assert kinds.index("state_machine") < kinds.index("role")                         # users before the roles they use
    assert kinds.index("glue_job") < kinds.index("role") and kinds.index("lambda") < kinds.index("role")
    assert ("role", "pf-github-deploy-role") in plan and ("glue_database", "pf_gold") in plan


def test_every_kind_in_the_plan_has_a_deleter():
    assert {kind for kind, _ in teardown.hand_built_resources(CONFIG)} <= set(teardown.DELETERS)


def test_the_hand_built_names_follow_the_prefix():
    names = {name for _, name in teardown.hand_built_resources({**CONFIG, "project_prefix": "pft"})}
    assert {"pft-new-batch-rule", "pft-batch-pipeline", "pft-ingest", "pft-wg", "pft-lambda-role"} <= names


def test_a_full_teardown_deletes_the_stacks_first_then_the_buckets_the_stacks_kept():
    plan = teardown.teardown_plan(CONFIG)
    assert plan == [("stack", "pf-pipeline"), ("stack", "pf-foundation"), ("bucket", "results"), ("bucket", "lake"),
                    ("stack", "pf-cicd")]


def test_a_role_is_stripped_of_its_policies_before_it_is_deleted():
    session = FakeSession(iam={"pages": {
        "list_attached_role_policies": {"AttachedPolicies": [{"PolicyArn": "arn:managed"}]},
        "list_role_policies": {"PolicyNames": ["inline-1"]}}})
    assert teardown.delete_role(session, "pf-lambda-role") is True
    calls = [method for _, method, _ in session.log]
    assert calls == ["list_attached_role_policies", "detach_role_policy", "list_role_policies", "delete_role_policy",
                     "delete_role"]


def test_deleting_something_that_is_already_gone_is_not_an_error():
    session = FakeSession(iam={"fail": {"list_attached_role_policies": "NoSuchEntity"}},
                          lambda_={}, glue={"fail": {"delete_job": "EntityNotFoundException"}})
    assert teardown.delete_role(session, "pf-lambda-role") is False
    assert teardown.delete_glue_job(session, "pf-bronze-to-silver") is False


def test_other_errors_are_not_hidden():
    session = FakeSession(glue={"fail": {"delete_job": "AccessDeniedException"}})
    with pytest.raises(ClientError):
        teardown.delete_glue_job(session, "pf-bronze-to-silver")


def test_a_bucket_is_emptied_with_all_its_versions_before_it_is_deleted():
    page = {"Versions": [{"Key": "a", "VersionId": "1"}, {"Key": "b", "VersionId": "2"}],
            "DeleteMarkers": [{"Key": "c", "VersionId": "3"}]}
    session = FakeSession(s3={"pages": {"list_object_versions": [page]}})
    assert teardown.empty_and_delete_bucket(session, "lake") is True

    calls = [(method, kwargs) for _, method, kwargs in session.log]
    methods = [method for method, _ in calls]
    assert methods == ["head_bucket", "list_object_versions", "delete_objects", "delete_bucket"]
    deleted = calls[2][1]["Delete"]["Objects"]
    assert deleted == [{"Key": "a", "VersionId": "1"}, {"Key": "b", "VersionId": "2"}, {"Key": "c", "VersionId": "3"}]


def test_a_missing_bucket_is_skipped():
    session = FakeSession(s3={"fail": {"head_bucket": "404"}})
    assert teardown.empty_and_delete_bucket(session, "lake") is False
    assert [method for _, method, _ in session.log] == ["head_bucket"]


def test_a_missing_stack_is_skipped():
    session = FakeSession(cloudformation={"fail": {"describe_stacks": "ValidationError"}})
    assert teardown.delete_stack(session, "pf-pipeline") is False
    assert [method for _, method, _ in session.log] == ["describe_stacks"]


def test_a_state_machine_is_found_by_name():
    pages = [{"stateMachines": [{"name": "other", "stateMachineArn": "arn:other"},
                                {"name": "pf-batch-pipeline", "stateMachineArn": "arn:mine"}]}]
    session = FakeSession(stepfunctions={"pages": {"list_state_machines": pages}})
    assert teardown.delete_state_machine(session, "pf-batch-pipeline") is True
    deleted = [kwargs for _, method, kwargs in session.log if method == "delete_state_machine"]
    assert deleted == [{"stateMachineArn": "arn:mine"}]
