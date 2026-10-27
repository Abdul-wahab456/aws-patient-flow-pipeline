"""Tests for infra/operations/teardown.py with fake AWS clients. Nothing is deleted anywhere."""
import pytest
from botocore.exceptions import ClientError

from infra.operations import teardown

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

    def get_waiter(self, name):
        recorder = self

        class Waiter:
            def wait(self, **kwargs):
                recorder.log.append((recorder.name, f"wait:{name}", kwargs))
        return Waiter()

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


def test_a_full_teardown_deletes_the_stacks_first_then_the_buckets_the_stacks_kept():
    plan = teardown.teardown_plan(CONFIG)
    assert plan == [("stack", "pf-pipeline"), ("stack", "pf-foundation"), ("bucket", "results"), ("bucket", "lake"),
                    ("stack", "pf-cicd")]


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


def test_other_errors_are_not_hidden():
    session = FakeSession(s3={"fail": {"head_bucket": "AccessDenied"}})
    with pytest.raises(ClientError):
        teardown.empty_and_delete_bucket(session, "lake")


def test_a_stack_is_deleted_and_waited_for():
    session = FakeSession(cloudformation={})
    assert teardown.delete_stack(session, "pf-pipeline") is True
    methods = [method for _, method, _ in session.log]
    assert methods == ["describe_stacks", "delete_stack", "wait:stack_delete_complete"]
