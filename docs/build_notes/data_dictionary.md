# Data Dictionary — Synthea FHIR Files

What is inside the raw files, and where each column of our **silver** tables comes from.
All numbers below were measured on the generated data (seed 42, 300 living patients, 5 years of history, Massachusetts).

## 1. The files

| File(s)                                                          . | Count | What it is                                                                                                                           |
| ------------------------------------------------------------------ | ----- | ------------------------------------------------------------------------------------------------------------------------------------ |
| `output/fhir/<Name>_<id>.json`                                     | 341   | One**Bundle** per patient: the patient's whole history (300 living + 41 deceased)                                                    |
| `output/fhir/hospitalInformation*.json`                            | 1     | Bundle of 521 `Organization` + 522 `Location` resources (the hospitals/clinics)                                                      |
| `output/fhir/practitionerInformation*.json`                        | 1     | Bundle of 521 `Practitioner` + 521 `PractitionerRole` resources (the doctors)                                                        |
| `output/metadata/*.json`                                           | 1     | **Run information only** (seed, patient count, run time, state). It does **not** describe columns, and the pipeline does not use it. |

### Anatomy of a patient file

```
{ "resourceType": "Bundle", "type": "transaction",
  "entry": [                       <- a list; each item is one record
     { "fullUrl": "urn:uuid:<id>",
       "resource": { "resourceType": "Patient" | "Encounter" | "Condition" | ... , ... },
       "request": { "method": "POST", "url": "Patient" } },
     ...
  ] }
```

The columns of our tables are the fields **inside `entry[].resource`**. `resourceType` tells us which table a record belongs to.

## 2. What is inside the patient bundles (all 341 files)

| resourceType                                                                                     | Count        | Used in our silver layer?                                  |
| ------------------------------------------------------------------------------------------------ | ------------ | ---------------------------------------------------------- |
| Observation                                                                                      | 80,258       | No (labs/vitals, largest resource)`[EXTRA]` possible later |
| Procedure                                                                                        | 28,284       | No                                                         |
| DiagnosticReport                                                                                 | 20,039       | No                                                         |
| Claim / ExplanationOfBenefit                                                                     | 19,481 each  | No                                                         |
| **Encounter**                                                                                    | **10,876**   | **Yes**                                                    |
| DocumentReference                                                                                | 10,876       | No                                                         |
| MedicationRequest                                                                                | 8,605        | No                                                         |
| **Condition**                                                                                    | **7,243**    | **Yes**                                                    |
| Medication / MedicationAdministration                                                            | 3,528 each   | No                                                         |
| Immunization                                                                                     | 2,323        | No                                                         |
| Other (Device, ImagingStudy, CareTeam, CarePlan, AllergyIntolerance, SupplyDelivery, Provenance) | < 5,000 each | No                                                         |
| **Patient**                                                                                      | **341**      | **Yes**                                                    |

The silver layer keeps only 5 resource types (Patient, Encounter, Condition, Organization, Practitioner) because the KPIs only need those.

## 3. Silver column → FHIR field mapping

Path notation: `a.b[0].c` = field `c` inside the first item of list `b` inside `a`. Every path starts inside `entry[].resource`.

### 3.1 `patient` (from `Patient`, 341 rows)

| Silver column     | FHIR path                      | Example / notes                                             |
| ----------------- | ------------------------------ | ----------------------------------------------------------- |
| patient_id        | `id`                           | `76a1559f-a83a-59b9-ed07-4efd4a0f1b39`                      |
| gender            | `gender`                       | `male` / `female`                                           |
| birth_date        | `birthDate`                    | `1980-01-16`                                                |
| deceased_datetime | `deceasedDateTime`             | present for**41** of 341 patients, otherwise missing (null) |
| marital_status    | `maritalStatus.coding[0].code` | `M` (display: Married)                                      |
| city              | `address[0].city`              | `Westfield`                                                 |
| state             | `address[0].state`             | `MA`                                                        |
| postal_code       | `address[0].postalCode`        | `01085`                                                     |

### 3.2 `encounter` (from `Encounter`, 10,876 rows)

| Silver column   | FHIR path                             | Example / notes                                                                                                                              |
| --------------- | ------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| encounter_id    | `id`                                  | uuid                                                                                                                                         |
| patient_id      | `subject.reference`                   | `urn:uuid:<patient id>` → strip `urn:uuid:`                                                                                                  |
| status          | `status`                              | always `finished` in this data                                                                                                               |
| encounter_class | `class.code`                          | `AMB` 10,135, `EMER` 373, `IMP` 283, `HH` 50, `VR` 35                                                                                        |
| type_code       | `type[0].coding[0].code`              | SNOMED code, e.g.`410620009`                                                                                                                 |
| type_display    | `type[0].coding[0].display`           | `Well child visit (procedure)`                                                                                                               |
| start_ts        | `period.start`                        | `1997-09-04T08:10:14+05:00`                                                                                                                  |
| end_ts          | `period.end`                          | present for all encounters                                                                                                                   |
| organization_id | `serviceProvider.reference`           | `Organization?identifier=<system>\|<id>` → take the part after `\|`                                                                          |
| practitioner_id | `participant[0].individual.reference` | `Practitioner?identifier=http://hl7.org/fhir/sid/us-npi\|9999982090` → the part after `\|` is the **NPI**, not the Practitioner `id` (see 4) |
| reason_code     | `reasonCode[0].coding[0].code`        | present on 6,761 of 10,876 encounters,**and on all 283 inpatient ones**                                                                      |
| reason_display  | `reasonCode[0].coding[0].display`     | `Dependent drug abuse (disorder)`                                                                                                            |

Class meanings: **AMB** ambulatory (outpatient visit), **EMER** emergency, **IMP** inpatient (hospital stay: the admissions/readmissions KPIs), **HH** home health, **VR** virtual.

### 3.3 `condition` (from `Condition`, 7,243 rows)

| Silver column   | FHIR path                       | Example / notes                                                 |
| --------------- | ------------------------------- | --------------------------------------------------------------- |
| condition_id    | `id`                            | uuid                                                            |
| patient_id      | `subject.reference`             | `urn:uuid:<id>` → strip prefix                                  |
| encounter_id    | `encounter.reference`           | `urn:uuid:<id>` → strip prefix                                  |
| code            | `code.coding[0].code`           | SNOMED code, e.g.`196416002`                                    |
| display         | `code.coding[0].display`        | `Impacted molars (disorder)`                                    |
| clinical_status | `clinicalStatus.coding[0].code` | `active` 2,893, `resolved` 4,350                                |
| onset_ts        | `onsetDateTime`                 | present on all                                                  |
| abatement_ts    | `abatementDateTime`             | present only on the 4,350 resolved conditions (null for active) |

### 3.4 `organization` (from `Organization` in the hospital file, 521 rows)

| Silver column              | FHIR path                                    | Example / notes                                                                                          |
| -------------------------- | -------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| organization_id            | `id`                                         | equals `identifier[0].value`, which is what encounters point to (confirmed with a join when the Glue job was built) |
| name                       | `name`                                       | `Fitchburg Outpatient Clinic`                                                                            |
| type                       | `type[0].coding[0].code`                     | `prov` (Healthcare Provider)                                                                             |
| city / state / postal_code | `address[0].city` / `.state` / `.postalCode` | `Fitchburg` / `MA` / `01420`                                                                             |

### 3.5 `practitioner` (from `Practitioner` in the practitioner file, 521 rows)

| Silver column   | FHIR path                          | Example / notes                                                                      |
| --------------- | ---------------------------------- | ------------------------------------------------------------------------------------ |
| practitioner_id | `id`                               | uuid                                                                                 |
| npi             | `identifier[0].value`              | `9999984591` (National Provider Identifier; system `http://hl7.org/fhir/sid/us-npi`) |
| full_name       | `name[0].given` + `name[0].family` | `Ted955 Reilly981` (Synthea appends digits to every name)                            |
| gender          | `gender`                           | `male`                                                                               |

## 4. Reference formats (how records point to each other)

| From                            | Field                                      | Format seen                                                                | How to turn it into a plain id                         |
| ------------------------------- | ------------------------------------------ | -------------------------------------------------------------------------- | ------------------------------------------------------ |
| Encounter → Patient             | `subject.reference`                        | `urn:uuid:<id>`                                                            | remove `urn:uuid:`                                     |
| Condition → Patient / Encounter | `subject.reference`, `encounter.reference` | `urn:uuid:<id>`                                                            | remove `urn:uuid:`                                     |
| Encounter → Organization        | `serviceProvider.reference`                | `Organization?identifier=https://github.com/synthetichealth/synthea\|<id>` | take the text after the last `\|`                      |
| Encounter → Practitioner        | `participant[0].individual.reference`      | `Practitioner?identifier=http://hl7.org/fhir/sid/us-npi\|<NPI>`            | take the text after the last `\|` (this is an **NPI**) |

**Finding that affects the design:** encounters identify the doctor by **NPI**, but the Practitioner record has its own uuid `id`. So `encounter.practitioner_id` holds an NPI and must be joined to `practitioner.npi`, not to `practitioner.practitioner_id`. Decision needed in the Glue job and gold layer: key the practitioner dimension on NPI, or look the uuid up through the NPI.

**Decision taken when the Glue job was built:** the silver `encounter` column is named **`practitioner_npi`** (not `practitioner_id`) and joins to `practitioner.npi`. Timestamps are stored as UTC instants.

## 5. Things to watch for in later phases

1. **Time zones.** Timestamps carry an offset, and the offsets in this data are `+05:00` (10,723), `+06:00` (106), `+05:30` (35) and `+06:30` (12). Spark converts such strings using its session time zone; the Glue job must set this on purpose (e.g. UTC) so `los_hours` is computed consistently.
2. **Only 283 inpatient encounters.** Enough to build and test the length-of-stay and 30-day readmission KPIs, but the readmission counts will be small. This is a property of the synthetic data, not a pipeline fault.
3. **Missing values are absent, not null.** A field with no value is simply not in the JSON (e.g. `deceasedDateTime`, `abatementDateTime`, `reasonCode`). Spark reads these as null.
4. **Nested lists.** `entry`, `type`, `coding`, `participant`, `reasonCode` are lists; the Glue job uses `explode` for `entry` and `[0]` (first item) for the rest.
5. **Names carry digits** (`Reilly981`) by design, so no real person can match.
