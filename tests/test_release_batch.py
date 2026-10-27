"""Tests for infra/release_batch.py with fake S3 and Step Functions clients. No AWS needed."""
import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "infra"))
import release_batch as rb  # noqa: E402


class FakePaginator:
    def __init__(self, keys):
        self.keys = keys

    def paginate(self, Bucket, Prefix):  # noqa: N803 (boto3 spelling)
        yield {"Contents": [{"Key": key} for key in self.keys if key.startswith(Prefix)]}


class FakeS3:
    def __init__(self, keys):
        self.keys, self.written = keys, []

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return FakePaginator(self.keys)

    def put_object(self, **kwargs):
        self.written.append(kwargs)


class FakeStepFunctions:
    def __init__(self, runs, statuses=("SUCCEEDED",)):
        self.runs, self.statuses = runs, list(statuses)

    def list_executions(self, stateMachineArn, maxResults):  # noqa: N803
        return {"executions": self.runs}

    def describe_execution(self, executionArn):  # noqa: N803
        return {"executionArn": executionArn, "status": self.statuses.pop(0)}


class FakeSession:
    def __init__(self, s3, sfn):
        self.clients = {"s3": s3, "stepfunctions": sfn}

    def client(self, name):
        return self.clients[name]


def started_just_now(name="run-1"):
    return {"executionArn": f"arn:{name}", "startDate": datetime.now(timezone.utc) + timedelta(seconds=1)}


def test_the_marker_is_not_counted_as_a_data_file():
    keys = ["landing/batch_02/a.json", "landing/batch_02/b.json", "landing/batch_02/_SUCCESS",
            "landing/batch_03/c.json"]
    assert rb.count_batch_files(FakeS3(keys), "lake", 2) == 2


def test_the_marker_announces_the_number_of_files_and_is_written_after_counting():
    s3 = FakeS3(["landing/batch_01/a.json", "landing/batch_01/b.json", "landing/batch_01/c.json"])
    run = rb.release_batch(FakeSession(s3, FakeStepFunctions([started_just_now()])), "lake", 1, "arn:machine")

    (written,) = s3.written
    assert written["Key"] == "landing/batch_01/_SUCCESS" and written["Bucket"] == "lake"
    assert json.loads(written["Body"]) == {"files": 3}
    assert run["status"] == "SUCCEEDED"


def test_an_announced_count_overrides_the_real_one_to_test_an_incomplete_delivery():
    s3 = FakeS3(["landing/batch_01/a.json"])
    rb.release_batch(FakeSession(s3, FakeStepFunctions([started_just_now()])), "lake", 1, "arn:machine",
                     announced_files=999)
    assert json.loads(s3.written[0]["Body"]) == {"files": 999}


def test_an_empty_batch_is_refused_before_anything_is_written():
    s3 = FakeS3([])
    with pytest.raises(ValueError, match="upload the batch first"):
        rb.release_batch(FakeSession(s3, FakeStepFunctions([])), "lake", 4, "arn:machine")
    assert s3.written == []


def test_a_failed_run_is_returned_so_the_caller_can_report_why():
    s3 = FakeS3(["landing/batch_01/a.json"])
    run = rb.release_batch(FakeSession(s3, FakeStepFunctions([started_just_now()], statuses=["FAILED"])), "lake", 1,
                           "arn:machine")
    assert run["status"] == "FAILED"


def test_waiting_goes_on_while_the_run_is_running(monkeypatch):
    monkeypatch.setattr(rb.time, "sleep", lambda seconds: None)
    sfn = FakeStepFunctions([], statuses=["RUNNING", "RUNNING", "SUCCEEDED"])
    assert rb.wait_for_run(sfn, "arn:x")["status"] == "SUCCEEDED"


def test_a_run_that_never_ends_times_out(monkeypatch):
    monkeypatch.setattr(rb.time, "sleep", lambda seconds: None)
    with pytest.raises(TimeoutError):
        rb.wait_for_run(FakeStepFunctions([], statuses=["RUNNING"] * 50), "arn:x", timeout=-1)


@pytest.mark.parametrize("runs", [[], ["a", "b"]])
def test_exactly_one_run_must_start(runs, monkeypatch):
    monkeypatch.setattr(rb.time, "sleep", lambda seconds: None)
    sfn = FakeStepFunctions([started_just_now(name) for name in runs])
    with pytest.raises(RuntimeError, match="exactly one execution"):
        rb.find_started_run(sfn, "arn:machine", datetime.now(timezone.utc), attempts=2)


def test_runs_that_started_before_the_marker_are_ignored(monkeypatch):
    monkeypatch.setattr(rb.time, "sleep", lambda seconds: None)
    old = {"executionArn": "arn:old", "startDate": datetime.now(timezone.utc) - timedelta(hours=1)}
    sfn = FakeStepFunctions([old, started_just_now("new")])
    assert rb.find_started_run(sfn, "arn:machine", datetime.now(timezone.utc))["executionArn"] == "arn:new"


# ---- any batch number, no fixed list --------------------------------------------------------------------------
@pytest.mark.parametrize("text", ["1", "4", "5", "05", "6", "42", "99", " 7 "])
def test_every_batch_number_from_1_to_99_is_accepted(text):
    assert rb.batch_number(text) == int(text)


@pytest.mark.parametrize("text", ["0", "100", "-1", "five", "", "1.5", "3; rm -rf /", "001"])
def test_other_batch_numbers_and_text_are_refused(text):
    with pytest.raises(argparse.ArgumentTypeError, match="1 to 99"):
        rb.batch_number(text)


class FakeListing:
    def __init__(self, prefixes):
        self.prefixes = prefixes

    def get_paginator(self, name):
        listing = self

        class Paginator:
            def paginate(self, Bucket, Prefix, Delimiter):  # noqa: N803 (boto3 spelling)
                assert (Prefix, Delimiter) == ("landing/", "/")
                yield {"CommonPrefixes": [{"Prefix": prefix} for prefix in listing.prefixes]}
        return Paginator()


def test_the_batches_in_landing_are_found_in_ascending_order_whatever_their_number():
    prefixes = ["landing/batch_10/", "landing/batch_02/", "landing/batch_05/", "landing/batch_01/",
                "landing/batch_5/", "landing/batch_100/", "landing/other/", "landing/batch_07x/"]
    assert rb.list_batches(FakeListing(prefixes), "lake") == [1, 2, 5, 10]


def test_a_new_batch_is_released_with_no_setting_that_names_how_many_batches_exist():
    s3 = FakeS3(["landing/batch_05/a.json", "landing/batch_05/b.json"])
    run = rb.release_batch(FakeSession(s3, FakeStepFunctions([started_just_now()])), "lake", 5, "arn:machine")
    assert s3.written[0]["Key"] == "landing/batch_05/_SUCCESS" and json.loads(s3.written[0]["Body"]) == {"files": 2}
    assert run["status"] == "SUCCEEDED"
