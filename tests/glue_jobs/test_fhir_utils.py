"""Tests for the silver flattening logic. They run Spark locally (no AWS) on small sample resources
that copy the shapes found in the real Synthea files."""
import json
import os
import sys
from datetime import date
from pathlib import Path

import pytest

pytest.importorskip("pyspark")
from pyspark.sql import SparkSession  # noqa: E402
from pyspark.sql import functions as F  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "glue_jobs"))
import fhir_utils as fu  # noqa: E402

NPI = "http://hl7.org/fhir/sid/us-npi"
SYNTHEA = "https://github.com/synthetichealth/synthea"


@pytest.fixture(scope="session")
def spark():
    # Spark starts helper Python processes; make sure they use the same Python as the tests
    # (on a machine with several Pythons it would otherwise pick another one and crash)
    os.environ["PYSPARK_PYTHON"] = sys.executable
    os.environ["PYSPARK_DRIVER_PYTHON"] = sys.executable
    session = (
        SparkSession.builder.master("local[1]").appName("fhir-tests")
        .config("spark.ui.enabled", "false")
        .config("spark.sql.shuffle.partitions", "1")
        .config("spark.sql.session.timeZone", "UTC")
        .getOrCreate()
    )
    session.sparkContext.setLogLevel("ERROR")
    yield session
    session.stop()


@pytest.fixture
def resources_df(spark, tmp_path):
    """Build a DataFrame like read_resources does (columns json + resource_type) from sample resources.

    The samples are written to a file and read back as text, the same route the real job takes. This also
    avoids Spark's Python helper process, which cannot start from a folder whose path contains a space.
    """
    counter = {"n": 0}

    def build(*resources):
        counter["n"] += 1
        path = tmp_path / f"resources_{counter['n']}.jsonl"
        path.write_text(os.linesep.join(json.dumps(r) for r in resources), encoding="utf-8")
        return (
            spark.read.text(str(path)).select(F.col("value").alias("json"))
            .withColumn("resource_type", F.get_json_object("json", "$.resourceType"))
        )

    return build


def rows(df):
    """Rows as dicts. Timestamps become 'yyyy-MM-dd HH:mm:ss' text in UTC.

    collect() would turn a timestamp into the time zone of the computer running the test, so the same
    test would pass on GitHub (UTC) and fail on a laptop in another time zone.
    """
    columns = [
        F.date_format(F.col(field.name), "yyyy-MM-dd HH:mm:ss").alias(field.name)
        if field.dataType.simpleString() == "timestamp" else F.col(field.name)
        for field in df.schema.fields
    ]
    return [row.asDict() for row in df.select(columns).collect()]


# ---- sample resources ------------------------------------------------------------------------
PATIENT = {
    "resourceType": "Patient", "id": "p1", "gender": "female", "birthDate": "1980-01-16",
    "maritalStatus": {"coding": [{"code": "M", "display": "Married"}]},
    "address": [{"city": "Westfield", "state": "MA", "postalCode": "01085", "line": ["1 Main St"]}],
}
DECEASED_PATIENT = {
    "resourceType": "Patient", "id": "p2", "gender": "male", "birthDate": "1950-03-02",
    "deceasedDateTime": "2020-05-01T10:00:00+05:00",
}
OFFICE_VISIT = {
    "resourceType": "Encounter", "id": "e1", "status": "finished",
    "class": {"system": "x", "code": "AMB"},
    "type": [{"coding": [{"code": "410620009", "display": "Well child visit (procedure)"}]}],
    "subject": {"reference": "urn:uuid:p1", "display": "Mrs. Test"},
    "participant": [{"individual": {"reference": f"Practitioner?identifier={NPI}|9999982090"}}],
    "period": {"start": "1997-09-04T08:10:14+05:00", "end": "1997-09-04T08:34:23+05:00"},
    "serviceProvider": {"reference": f"Organization?identifier={SYNTHEA}|org1"},
}
HOSPITAL_STAY = {
    "resourceType": "Encounter", "id": "e2", "status": "finished", "class": {"code": "IMP"},
    "type": [{"coding": [{"code": "56876005", "display": "Drug rehabilitation"}]}],
    "subject": {"reference": "urn:uuid:p1"},
    "participant": [{"individual": {"reference": f"Practitioner?identifier={NPI}|9999985994"}}],
    "period": {"start": "2017-11-09T08:10:14+05:00", "end": "2017-11-12T08:10:14+05:00"},
    "reasonCode": [{"coding": [{"code": "6525002", "display": "Dependent drug abuse (disorder)"}]}],
    "serviceProvider": {"reference": f"Organization?identifier={SYNTHEA}|org1"},
}
ACTIVE_CONDITION = {
    "resourceType": "Condition", "id": "c1",
    "clinicalStatus": {"coding": [{"code": "active"}]},
    "code": {"coding": [{"code": "196416002", "display": "Impacted molars (disorder)"}], "text": "x"},
    "subject": {"reference": "urn:uuid:p1"}, "encounter": {"reference": "urn:uuid:e1"},
    "onsetDateTime": "1997-09-06T08:10:14+05:00",
}
RESOLVED_CONDITION = {
    **ACTIVE_CONDITION, "id": "c2", "clinicalStatus": {"coding": [{"code": "resolved"}]},
    "abatementDateTime": "1997-10-06T08:10:14+05:00",
}
ORGANIZATION = {
    "resourceType": "Organization", "id": "org1", "name": "Fitchburg Outpatient Clinic",
    "type": [{"coding": [{"code": "prov", "display": "Healthcare Provider"}]}],
    "address": [{"line": ["881 Main Street"], "city": "Fitchburg", "state": "MA", "postalCode": "01420"}],
}
PRACTITIONER = {
    "resourceType": "Practitioner", "id": "pr1", "gender": "male",
    "identifier": [
        {"system": "http://other.example/id", "value": "not-an-npi"},
        {"system": NPI, "value": "9999984591"},
    ],
    "name": [{"family": "Reilly981", "given": ["Ted955"], "prefix": ["Dr."]}],
}
# Claim.type is an OBJECT while Encounter.type is a LIST: this is why resources are parsed one type at a time
CLAIM = {"resourceType": "Claim", "id": "cl1", "type": {"coding": [{"code": "institutional"}]}}


# ---- plain Python helpers ------------------------------------------------------------------------
def test_validate_ingest_date_accepts_a_real_date():
    assert fu.validate_ingest_date("2026-11-01") == "2026-11-01"


@pytest.mark.parametrize("value", ["2026-13-45", "2026-1-1", "20261101", "", None, "not-a-date"])
def test_validate_ingest_date_rejects_bad_values(value):
    with pytest.raises(ValueError):
        fu.validate_ingest_date(value)


def test_classify_bronze_files_splits_by_file_name_and_ignores_helper_files():
    keys = [
        "bronze/patient_records/ingest_date=2026-11-01/Abe604_Rogahn59_x.json",
        "bronze/patient_records/ingest_date=2026-11-01/hospitalInformation1792681729651.json",
        "bronze/patient_records/ingest_date=2026-11-01/practitionerInformation1792681729651.json",
        "bronze/patient_records/ingest_date=2026-11-01/_batch_metadata.json",
        "bronze/patient_records/ingest_date=2026-11-01/notes.txt",
        "bronze/patient_records/ingest_date=2026-11-01/Zed1_Last2_y.json",
    ]
    result = fu.classify_bronze_files(keys)
    assert [k.rsplit("/", 1)[1] for k in result["patients"]] == ["Abe604_Rogahn59_x.json", "Zed1_Last2_y.json"]
    assert len(result["hospital"]) == 1 and len(result["practitioners"]) == 1


def test_table_ddl_describes_a_partitioned_parquet_table():
    ddl = fu.table_ddl("pf_silver", "patient", [("patient_id", "string"), ("birth_date", "date")], "s3://b/silver/patient/")
    assert "CREATE EXTERNAL TABLE IF NOT EXISTS `pf_silver`.`patient`" in ddl
    assert "`patient_id` string" in ddl and "`birth_date` date" in ddl
    assert "PARTITIONED BY (`ingest_date` string)" in ddl
    assert "STORED AS PARQUET" in ddl and "LOCATION 's3://b/silver/patient/'" in ddl


# ---- Spark helpers ----------------------------------------------------------------------------------
def test_resolve_reference_handles_both_reference_styles(spark):
    df = spark.sql(
        "SELECT * FROM VALUES ('urn:uuid:abc-1'), "
        f"('Organization?identifier={SYNTHEA}|org-9'), ('Practitioner?identifier={NPI}|999'), "
        "('plain-id'), (CAST(NULL AS STRING)) AS t(ref)"
    )
    result = [r[0] for r in df.select(fu.resolve_reference(df["ref"])).collect()]
    assert result == ["abc-1", "org-9", "999", "plain-id", None]


def test_read_resources_reads_bundles_whose_resources_have_different_shapes(spark, tmp_path):
    bundle = {"resourceType": "Bundle", "entry": [{"resource": r} for r in (PATIENT, OFFICE_VISIT, CLAIM)]}
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(bundle, indent=2))      # multi-line file, like Synthea's output

    resources = fu.read_resources(spark, [str(path)])

    assert sorted(r["resource_type"] for r in rows(resources)) == ["Claim", "Encounter", "Patient"]
    assert fu.encounter_table(resources).count() == 1      # the Claim's different "type" shape did not break parsing


def test_require_no_nulls_passes_and_fails(spark):
    df = spark.sql("SELECT * FROM VALUES ('a', 'x'), ('b', CAST(NULL AS STRING)) AS t(id, other)")
    fu.require_no_nulls(df, ["id"], "demo")
    with pytest.raises(ValueError, match="demo: null values in required columns: {'other': 1}"):
        fu.require_no_nulls(df, ["id", "other"], "demo")


def test_require_no_nulls_accepts_an_empty_table(spark):
    fu.require_no_nulls(spark.sql("SELECT CAST(NULL AS STRING) AS id WHERE 1 = 0"), ["id"], "empty")


# ---- the five tables ------------------------------------------------------------------------------------
def test_patient_table(resources_df):
    patients = fu.patient_table(resources_df(PATIENT, DECEASED_PATIENT, OFFICE_VISIT))
    by_id = {r["patient_id"]: r for r in rows(patients)}

    assert set(by_id) == {"p1", "p2"}                       # the Encounter was ignored
    assert by_id["p1"] == {
        "patient_id": "p1", "gender": "female", "birth_date": date(1980, 1, 16), "deceased_datetime": None,
        "marital_status": "M", "city": "Westfield", "state": "MA", "postal_code": "01085",
    }
    assert by_id["p2"]["deceased_datetime"] == "2020-05-01 05:00:00"             # 10:00 +05:00 = 05:00 UTC
    assert by_id["p2"]["marital_status"] is None and by_id["p2"]["city"] is None  # missing fields become null
    assert dict(fu.columns_of(patients))["birth_date"] == "date"
    assert dict(fu.columns_of(patients))["deceased_datetime"] == "timestamp"


def test_encounter_table(resources_df):
    encounters = fu.encounter_table(resources_df(OFFICE_VISIT, HOSPITAL_STAY, PATIENT, CLAIM))
    by_id = {r["encounter_id"]: r for r in rows(encounters)}

    assert set(by_id) == {"e1", "e2"}
    assert by_id["e1"] == {
        "encounter_id": "e1", "patient_id": "p1", "status": "finished", "encounter_class": "AMB",
        "type_code": "410620009", "type_display": "Well child visit (procedure)",
        "start_ts": "1997-09-04 03:10:14", "end_ts": "1997-09-04 03:34:23",                       # +05:00 -> UTC
        "organization_id": "org1", "practitioner_npi": "9999982090", "reason_code": None, "reason_display": None,
    }
    assert by_id["e2"]["encounter_class"] == "IMP"
    assert by_id["e2"]["reason_code"] == "6525002"
    assert by_id["e2"]["reason_display"] == "Dependent drug abuse (disorder)"


def test_condition_table(resources_df):
    conditions = fu.condition_table(resources_df(ACTIVE_CONDITION, RESOLVED_CONDITION, PATIENT))
    by_id = {r["condition_id"]: r for r in rows(conditions)}

    assert by_id["c1"] == {
        "condition_id": "c1", "patient_id": "p1", "encounter_id": "e1", "code": "196416002",
        "display": "Impacted molars (disorder)", "clinical_status": "active",
        "onset_ts": "1997-09-06 03:10:14", "abatement_ts": None,
    }
    assert by_id["c2"]["clinical_status"] == "resolved"
    assert by_id["c2"]["abatement_ts"] == "1997-10-06 03:10:14"


def test_organization_table(resources_df):
    organizations = fu.organization_table(resources_df(ORGANIZATION, PRACTITIONER))
    assert rows(organizations) == [{
        "organization_id": "org1", "name": "Fitchburg Outpatient Clinic", "type": "prov",
        "city": "Fitchburg", "state": "MA", "postal_code": "01420",
    }]


def test_practitioner_table_picks_the_npi_identifier_and_builds_the_name(resources_df):
    no_given_name = {**PRACTITIONER, "id": "pr2", "name": [{"family": "Smith", "given": []}]}
    practitioners = fu.practitioner_table(resources_df(PRACTITIONER, no_given_name, ORGANIZATION))
    by_id = {r["practitioner_id"]: r for r in rows(practitioners)}

    assert by_id["pr1"] == {
        "practitioner_id": "pr1", "npi": "9999984591", "full_name": "Ted955 Reilly981", "gender": "male",
    }
    assert by_id["pr2"]["full_name"] == "Smith"             # no stray leading space


def test_a_repeated_resource_is_kept_once(resources_df):
    patients = fu.patient_table(resources_df(PATIENT, PATIENT))
    assert patients.count() == 1


def test_silver_tables_have_the_required_columns(resources_df):
    resources = resources_df(PATIENT, OFFICE_VISIT, ACTIVE_CONDITION, ORGANIZATION, PRACTITIONER)
    tables = {
        "patient": fu.patient_table(resources), "encounter": fu.encounter_table(resources),
        "condition": fu.condition_table(resources), "organization": fu.organization_table(resources),
        "practitioner": fu.practitioner_table(resources),
    }
    for name, df in tables.items():
        assert set(fu.REQUIRED_COLUMNS[name]) <= set(df.columns), name
        fu.require_no_nulls(df, fu.REQUIRED_COLUMNS[name], name)
