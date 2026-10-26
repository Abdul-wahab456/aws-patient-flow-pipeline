"""Unit tests for the ingest Lambda. They use a tiny fake S3 client, so no AWS access is needed."""
import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lambda" / "ingest"))
import handler  # noqa: E402

FIRST_DATE = date(2026, 11, 1)


class FakeS3:
    """Just enough of the S3 client for the handler: list, copy and put."""

    def __init__(self, objects):
        self.objects = dict(objects)  # key -> size in bytes
        self.copied = []
        self.put = {}

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
