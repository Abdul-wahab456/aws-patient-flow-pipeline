"""Unit tests for the ingest Lambda. They use a tiny fake S3 client, so no AWS access is needed."""
import io
import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lambda" / "ingest"))
import handler  # noqa: E402

FIRST_DATE = date(2026, 11, 1)


class FakeS3:
    """Just enough of the S3 client for the handler: list, read, copy and put."""

    def __init__(self, objects, bodies=None):
        self.objects = dict(objects)  # key -> size in bytes
        self.bodies = dict(bodies or {})  # key -> content, for files the handler reads (the marker)
        self.copied = []
        self.put = {}

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.bodies[Key])}

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return self

    def paginate(self, Bucket, Prefix):
        yield {"Contents": [{"Key": k, "Size": s} for k, s in self.objects.items() if k.startswith(Prefix)]}

    def copy_object(self, Bucket, Key, CopySource):
        self.copied.append((CopySource["Key"], Key))

    def put_object(self, Bucket, Key, Body, ContentType):
        self.put[Key] = json.loads(Body)


def test_parse_batch_accepts_valid_numbers():
    assert handler.parse_batch({"batch": 1}, 10) == 1
    assert handler.parse_batch({"batch": 10}, 10) == 10


@pytest.mark.parametrize("event", [{"batch": 0}, {"batch": 11}, {"batch": -1}, {"batch": "1"},
                                   {"batch": True}, {"batch": None}, {}, None, "1"])
def test_parse_batch_rejects_bad_input(event):
    with pytest.raises(ValueError):
        handler.parse_batch(event, 10)


def test_ingest_date_is_one_day_per_batch():
    assert handler.ingest_date_for(1, FIRST_DATE) == date(2026, 11, 1)
    assert handler.ingest_date_for(10, FIRST_DATE) == date(2026, 11, 10)


def test_ingest_batch_copies_files_and_writes_metadata():
    s3 = FakeS3({
        "landing/batch_02/a.json": 100,
        "landing/batch_02/b.json": 50,
        "landing/batch_03/other.json": 999,     # a different batch: must not be copied
    })
    result = handler.ingest_batch(s3, "bucket", 2, FIRST_DATE)

    assert result == {"status": "OK", "batch": 2, "ingest_date": "2026-11-02", "file_count": 2}
    assert sorted(s3.copied) == [
        ("landing/batch_02/a.json", "bronze/patient_records/ingest_date=2026-11-02/a.json"),
        ("landing/batch_02/b.json", "bronze/patient_records/ingest_date=2026-11-02/b.json"),
    ]
    metadata = s3.put["bronze/patient_records/ingest_date=2026-11-02/_batch_metadata.json"]
    assert metadata["file_count"] == 2 and metadata["total_bytes"] == 150 and metadata["batch"] == 2


def test_ingest_batch_fails_on_empty_batch():
    with pytest.raises(RuntimeError, match="no files found"):
        handler.ingest_batch(FakeS3({}), "bucket", 1, FIRST_DATE)


def test_ingest_batch_twice_gives_the_same_result():
    s3 = FakeS3({"landing/batch_01/a.json": 10})
    first = handler.ingest_batch(s3, "bucket", 1, FIRST_DATE)
    second = handler.ingest_batch(s3, "bucket", 1, FIRST_DATE)
    assert first == second
    assert len(set(s3.copied)) == 1          # same source and target both times


# ---- the entry point AWS calls: reads settings from environment variables and reports errors ----

def _set_environment(monkeypatch, s3):
    monkeypatch.setenv("LAKE_BUCKET", "bucket")
    monkeypatch.setenv("FIRST_INGEST_DATE", "2026-11-01")
    monkeypatch.setenv("NUMBER_OF_BATCHES", "4")
    monkeypatch.setattr(handler.boto3, "client", lambda service: s3)


def test_lambda_handler_runs_a_batch_from_the_environment_settings(monkeypatch):
    s3 = FakeS3({"landing/batch_01/a.json": 10})
    _set_environment(monkeypatch, s3)

    result = handler.lambda_handler({"batch": 1}, None)

    assert result == {"status": "OK", "batch": 1, "ingest_date": "2026-11-01", "file_count": 1}


def test_lambda_handler_rejects_a_batch_above_the_configured_maximum(monkeypatch):
    _set_environment(monkeypatch, FakeS3({}))

    with pytest.raises(ValueError, match="between 1 and 4"):
        handler.lambda_handler({"batch": 5}, None)


def test_lambda_handler_reraises_so_the_caller_sees_the_failure(monkeypatch):
    _set_environment(monkeypatch, FakeS3({}))      # batch 1 exists as a number but has no files

    with pytest.raises(RuntimeError, match="no files found"):
        handler.lambda_handler({"batch": 1}, None)


# ---- the marker file: "this delivery is complete" -----------------------------------------------------------
MARKER_KEY = "landing/batch_02/_SUCCESS"


def marker_event(key=MARKER_KEY, bucket="bucket"):
    return {"bucket": bucket, "key": key}


def delivered_batch_2(announced_files):
    """Batch 2 with two data files and a marker announcing `announced_files` files."""
    return FakeS3(
        {"landing/batch_02/a.json": 100, "landing/batch_02/b.json": 50, MARKER_KEY: 12},
        bodies={MARKER_KEY: json.dumps({"files": announced_files}).encode()},
    )


def test_parse_batch_reads_the_batch_number_from_a_marker_key():
    assert handler.parse_batch(marker_event("landing/batch_03/_SUCCESS"), 4) == 3


@pytest.mark.parametrize("key", [
    "landing/batch_03/patient.json",    # a data file, not the marker
    "bronze/batch_03/_SUCCESS",         # wrong folder
    "landing/batch_3/_SUCCESS",         # batch number must have two digits
    "landing/batch_03/x/_SUCCESS",      # marker must sit directly in the batch folder
    "landing/batch_05/_SUCCESS",        # batch 5 does not exist (4 batches)
    "landing/batch_00/_SUCCESS",
])
def test_parse_batch_rejects_keys_that_are_not_a_valid_marker(key):
    with pytest.raises(ValueError):
        handler.parse_batch(marker_event(key), 4)


def test_read_expected_files_returns_the_announced_count():
    s3 = FakeS3({}, bodies={MARKER_KEY: b'{"files": 37}'})
    assert handler.read_expected_files(s3, "bucket", MARKER_KEY) == 37


@pytest.mark.parametrize("body", [b"not json", b"{}", b'{"files": 0}', b'{"files": "37"}', b'{"files": true}', b"[]"])
def test_read_expected_files_rejects_a_bad_marker(body):
    with pytest.raises(ValueError, match="marker"):
        handler.read_expected_files(FakeS3({}, bodies={MARKER_KEY: body}), "bucket", MARKER_KEY)


def test_the_marker_is_neither_counted_nor_copied():
    s3 = delivered_batch_2(announced_files=2)
    result = handler.ingest_batch(s3, "bucket", 2, FIRST_DATE, expected_files=2)

    assert result["file_count"] == 2
    assert sorted(source for source, _ in s3.copied) == ["landing/batch_02/a.json", "landing/batch_02/b.json"]


def test_a_batch_with_a_different_number_of_files_than_announced_is_rejected_before_anything_is_copied():
    s3 = delivered_batch_2(announced_files=3)
    with pytest.raises(RuntimeError, match="incomplete batch 2: the marker announces 3 files but .* holds 2"):
        handler.ingest_batch(s3, "bucket", 2, FIRST_DATE, expected_files=3)
    assert s3.copied == [] and s3.put == {}


def test_lambda_handler_ingests_a_batch_announced_by_a_marker(monkeypatch):
    _set_environment(monkeypatch, delivered_batch_2(announced_files=2))
    result = handler.lambda_handler(marker_event(), None)
    assert result == {"status": "OK", "batch": 2, "ingest_date": "2026-11-02", "file_count": 2}


def test_lambda_handler_rejects_an_incomplete_delivery(monkeypatch):
    _set_environment(monkeypatch, delivered_batch_2(announced_files=5))
    with pytest.raises(RuntimeError, match="incomplete batch"):
        handler.lambda_handler(marker_event(), None)


def test_lambda_handler_refuses_a_marker_from_another_bucket(monkeypatch):
    _set_environment(monkeypatch, delivered_batch_2(announced_files=2))
    with pytest.raises(ValueError, match="this pipeline serves"):
        handler.lambda_handler(marker_event(bucket="somebody-elses-bucket"), None)


def test_a_manual_batch_start_still_works_when_a_marker_is_present(monkeypatch):
    s3 = delivered_batch_2(announced_files=999)         # the marker is ignored for {"batch": N} runs
    _set_environment(monkeypatch, s3)
    assert handler.lambda_handler({"batch": 2}, None)["file_count"] == 2
