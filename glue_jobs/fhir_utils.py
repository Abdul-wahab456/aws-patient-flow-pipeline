"""Flatten FHIR resources into the silver tables.

Used by the Glue job (glue_jobs/bronze_to_silver.py). Everything here takes and
returns Spark DataFrames or plain Python values and never touches AWS, so the
logic can be tested on a laptop with a local Spark session (tests/test_fhir_utils.py).

How a bundle file becomes silver tables:
    bundle file --read--> one row per resource (JSON text)
                --filter by resourceType--> parse with that type's schema
                --select/rename/cast--> one silver table

Why each resource is read as TEXT first: FHIR reuses field names with different
shapes (Claim.type is an object, Encounter.type is a list). One combined schema
would fail on such files, so each resource type is parsed with its own schema.
"""
import re
from datetime import date

from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F
from pyspark.sql.types import ArrayType, StringType, StructField, StructType

NPI_SYSTEM = "http://hl7.org/fhir/sid/us-npi"
HOSPITAL_FILE_PREFIX = "hospitalInformation"
PRACTITIONER_FILE_PREFIX = "practitionerInformation"

# Columns that must never be null in a silver table; the job fails instead of writing bad data
REQUIRED_COLUMNS = {
    "patient": ["patient_id"],
    "encounter": ["encounter_id", "patient_id", "encounter_class", "start_ts"],
    "condition": ["condition_id", "patient_id", "encounter_id"],
    "organization": ["organization_id"],
    "practitioner": ["practitioner_id", "npi"],
}


# ----------------------------------------------------------------------------------------------
# Schemas: only the fields we keep. Fields not listed here are ignored by Spark.
# ----------------------------------------------------------------------------------------------
def _struct(*fields):
    return StructType([StructField(name, dtype) for name, dtype in fields])


_STR = StringType()
_CODING = ArrayType(_struct(("code", _STR), ("display", _STR)))
_CONCEPT = _struct(("coding", _CODING))                         # FHIR "CodeableConcept"
_REFERENCE = _struct(("reference", _STR))
_ADDRESS = ArrayType(_struct(("city", _STR), ("state", _STR), ("postalCode", _STR)))

# A bundle file: {"entry": [{"resource": {...}}, ...]}; each resource is kept as JSON text
BUNDLE_SCHEMA = _struct(("entry", ArrayType(_struct(("resource", _STR)))))

PATIENT_SCHEMA = _struct(
    ("id", _STR), ("gender", _STR), ("birthDate", _STR), ("deceasedDateTime", _STR),
    ("maritalStatus", _CONCEPT), ("address", _ADDRESS),
)
ENCOUNTER_SCHEMA = _struct(
    ("id", _STR), ("status", _STR), ("class", _struct(("code", _STR))),
    ("type", ArrayType(_CONCEPT)), ("subject", _REFERENCE),
    ("participant", ArrayType(_struct(("individual", _REFERENCE)))),
    ("period", _struct(("start", _STR), ("end", _STR))),
    ("reasonCode", ArrayType(_CONCEPT)), ("serviceProvider", _REFERENCE),
)
CONDITION_SCHEMA = _struct(
    ("id", _STR), ("subject", _REFERENCE), ("encounter", _REFERENCE), ("code", _CONCEPT),
    ("clinicalStatus", _CONCEPT), ("onsetDateTime", _STR), ("abatementDateTime", _STR),
)
ORGANIZATION_SCHEMA = _struct(
    ("id", _STR), ("name", _STR), ("type", ArrayType(_CONCEPT)), ("address", _ADDRESS),
)
PRACTITIONER_SCHEMA = _struct(
    ("id", _STR),
    ("identifier", ArrayType(_struct(("system", _STR), ("value", _STR)))),
    ("name", ArrayType(_struct(("family", _STR), ("given", ArrayType(_STR))))),
    ("gender", _STR),
)


# ----------------------------------------------------------------------------------------------
# Plain Python helpers (no Spark needed)
# ----------------------------------------------------------------------------------------------
def validate_ingest_date(value):
    """Return the value if it is a real date written YYYY-MM-DD; otherwise raise ValueError."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value or ""):
        raise ValueError(f"ingest_date must look like YYYY-MM-DD, got {value!r}")
    date.fromisoformat(value)       # rejects impossible dates such as 2026-13-45
    return value


def classify_bronze_files(keys):
    """Split the file keys of one bronze folder into patient bundles, hospital file and practitioner file.

    Files whose name starts with an underscore (such as _batch_metadata.json) and files
    that are not .json are not FHIR data and are ignored.
    """
    result = {"patients": [], "hospital": [], "practitioners": []}
    for key in sorted(keys):
        name = key.rsplit("/", 1)[-1]
        if name.startswith("_") or not name.endswith(".json"):
            continue
        if name.startswith(HOSPITAL_FILE_PREFIX):
            result["hospital"].append(key)
        elif name.startswith(PRACTITIONER_FILE_PREFIX):
            result["practitioners"].append(key)
        else:
            result["patients"].append(key)
    return result


def columns_of(df):
    """(name, Hive type) pairs of a DataFrame, e.g. [('patient_id', 'string'), ('birth_date', 'date')]."""
    return [(field.name, field.dataType.simpleString()) for field in df.schema.fields]


def table_ddl(database, table, columns, location):
    """CREATE TABLE statement for a silver table: Parquet files, partitioned by ingest_date.

    ingest_date is not stored inside the files; it comes from the folder name ingest_date=YYYY-MM-DD.
    """
    column_lines = ",\n  ".join(f"`{name}` {dtype}" for name, dtype in columns)
    return (
        f"CREATE EXTERNAL TABLE IF NOT EXISTS `{database}`.`{table}` (\n  {column_lines}\n)\n"
        f"PARTITIONED BY (`ingest_date` string)\n"
        f"STORED AS PARQUET\n"
        f"LOCATION '{location}'"
    )


# ----------------------------------------------------------------------------------------------
# Spark helpers
# ----------------------------------------------------------------------------------------------
def read_resources(spark: SparkSession, paths) -> DataFrame:
    """Read bundle files; return one row per resource with columns json (text) and resource_type."""
    bundles = spark.read.schema(BUNDLE_SCHEMA).option("multiLine", True).json(list(paths))
    return (
        bundles.select(F.explode("entry").alias("entry"))
        .select(F.col("entry.resource").alias("json"))
        .withColumn("resource_type", F.get_json_object("json", "$.resourceType"))
    )


def _parse(resources, resource_type, schema):
    """Keep one resource type and turn its JSON text into columns using its own schema."""
    return (
        resources.filter(F.col("resource_type") == resource_type)
        .select(F.from_json("json", schema).alias("r"))
        .select("r.*")
    )


def resolve_reference(reference):
    """Column expression: turn a FHIR reference into a plain id.

    urn:uuid:<id>                            -> <id>
    Organization?identifier=<system>|<id>    -> <id>   (conditional reference)
    Practitioner?identifier=<system>|<npi>   -> <npi>
    """
    return (
        F.when(reference.startswith("urn:uuid:"), F.regexp_replace(reference, "^urn:uuid:", ""))
        .when(reference.contains("|"), F.regexp_extract(reference, r"\|([^|]*)$", 1))
        .otherwise(reference)
    )


def require_no_nulls(df, columns, table):
    """Raise ValueError if any of the columns contains a null (fail instead of writing bad data)."""
    counts = df.select([F.sum(F.col(c).isNull().cast("int")).alias(c) for c in columns]).first()
    bad = {c: counts[c] for c in columns if counts[c]}
    if bad:
        raise ValueError(f"{table}: null values in required columns: {bad}")


# ----------------------------------------------------------------------------------------------
# The five silver tables. Timestamps keep their instant: "...T08:10:14+05:00" becomes 03:10:14 UTC.
# Within one batch a repeated id is kept once (dropDuplicates).
# ----------------------------------------------------------------------------------------------
def patient_table(resources):
    patient = _parse(resources, "Patient", PATIENT_SCHEMA)
    address = F.col("address")[0]
    return patient.select(
        F.col("id").alias("patient_id"),
        F.col("gender"),
        F.to_date("birthDate").alias("birth_date"),
        F.to_timestamp("deceasedDateTime").alias("deceased_datetime"),
        F.col("maritalStatus")["coding"][0]["code"].alias("marital_status"),
        address["city"].alias("city"),
        address["state"].alias("state"),
        address["postalCode"].alias("postal_code"),
    ).dropDuplicates(["patient_id"])


def encounter_table(resources):
    encounter = _parse(resources, "Encounter", ENCOUNTER_SCHEMA)
    return encounter.select(
        F.col("id").alias("encounter_id"),
        resolve_reference(F.col("subject")["reference"]).alias("patient_id"),
        F.col("status"),
        F.col("class")["code"].alias("encounter_class"),
        F.col("type")[0]["coding"][0]["code"].alias("type_code"),
        F.col("type")[0]["coding"][0]["display"].alias("type_display"),
        F.to_timestamp(F.col("period")["start"]).alias("start_ts"),
        F.to_timestamp(F.col("period")["end"]).alias("end_ts"),
        resolve_reference(F.col("serviceProvider")["reference"]).alias("organization_id"),
        # The doctor is referenced by NPI (national provider number), not by the Practitioner's own id
        resolve_reference(F.col("participant")[0]["individual"]["reference"]).alias("practitioner_npi"),
        F.col("reasonCode")[0]["coding"][0]["code"].alias("reason_code"),
        F.col("reasonCode")[0]["coding"][0]["display"].alias("reason_display"),
    ).dropDuplicates(["encounter_id"])


def condition_table(resources):
    condition = _parse(resources, "Condition", CONDITION_SCHEMA)
    return condition.select(
        F.col("id").alias("condition_id"),
        resolve_reference(F.col("subject")["reference"]).alias("patient_id"),
        resolve_reference(F.col("encounter")["reference"]).alias("encounter_id"),
        F.col("code")["coding"][0]["code"].alias("code"),
        F.col("code")["coding"][0]["display"].alias("display"),
        F.col("clinicalStatus")["coding"][0]["code"].alias("clinical_status"),
        F.to_timestamp("onsetDateTime").alias("onset_ts"),
        F.to_timestamp("abatementDateTime").alias("abatement_ts"),
    ).dropDuplicates(["condition_id"])


def organization_table(resources):
    organization = _parse(resources, "Organization", ORGANIZATION_SCHEMA)
    address = F.col("address")[0]
    return organization.select(
        F.col("id").alias("organization_id"),
        F.col("name"),
        F.col("type")[0]["coding"][0]["code"].alias("type"),
        address["city"].alias("city"),
        address["state"].alias("state"),
        address["postalCode"].alias("postal_code"),
    ).dropDuplicates(["organization_id"])


def practitioner_table(resources):
    practitioner = _parse(resources, "Practitioner", PRACTITIONER_SCHEMA)
    name = F.col("name")[0]
    npi = F.filter("identifier", lambda i: i["system"] == NPI_SYSTEM)[0]["value"]
    return practitioner.select(
        F.col("id").alias("practitioner_id"),
        npi.alias("npi"),
        F.trim(F.concat_ws(" ", F.concat_ws(" ", name["given"]), name["family"])).alias("full_name"),
        F.col("gender"),
    ).dropDuplicates(["practitioner_id"])
