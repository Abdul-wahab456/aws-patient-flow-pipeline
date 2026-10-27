# Generate & Explore the Data (local, free)

## Goal

Create realistic **synthetic** hospital data on this laptop, split it into 10 "daily" batches that the cloud pipeline will ingest one by one, and understand its structure well enough to write the Silver-layer code in the Glue job. No AWS resources and no cost.

## Concepts

- **Synthea**: an open-source generator of synthetic patients and their medical histories. No real patient data is involved, ever.
- **FHIR (Fast Healthcare Interoperability Resources)**: the standard JSON format for healthcare data. Everything is a _resource_ with a `resourceType` (Patient, Encounter, Condition, Organization, Practitioner, ...).
- **Transaction Bundle**: one JSON file that wraps many resources in an `entry[]` list. Synthea writes **one bundle per patient** (that patient's whole history), plus separate bundles for hospitals and practitioners.
- **Reference**: how one resource points to another (e.g. an Encounter points to its Patient). Synthea uses two styles (`urn:uuid:<id>` and conditional references like `Organization?identifier=...|<id>`), so the Glue job needs a helper to turn both into plain IDs.
- **Batch**: a group of patient files that simulates "one day's data arriving". Batch N arrives on `2026-11-01 + (N-1) days`.

## Tools

| Tool                              | Needed for                | Status                                  |
| --------------------------------- | ------------------------- | --------------------------------------- |
| Java JDK 17+                      | Synthea is a Java program | Not installed at the start of the phase |
| `synthea-with-dependencies.jar` | the generator itself      | to download                             |

## Facts verified from Synthea's documentation

- Java **17 or newer**, and it must be a **JDK**, not a JRE.
- Defaults already on: FHIR export, hospital export, practitioner export, transaction bundles. Default years of history is 10 and default output folder is `./output/`.
- So our plan's flag `--exporter.fhir.export true` is redundant but harmless; the flag we really need is `--exporter.years_of_history 5`.

## Steps you run

### Step 1 — Install Java (JDK 17)

```powershell
winget install --id Microsoft.OpenJDK.17 -e
```

- Same pattern as the AWS CLI install: `winget` downloads the official package and adds it to `PATH`. `-e` = exact id match.
- `Microsoft.OpenJDK.17` = Microsoft's build of OpenJDK 17, a free JDK.
- **Close the terminal and open a new one afterwards**, so `PATH` is re-read. Then re-activate the venv (`.venv\Scripts\Activate.ps1`) and check:

```powershell
java -version
```

Expected: a line containing `openjdk version "17.0.x"`.

- **Result:** `openjdk 17.0.20.1 2026-08-18 LTS` (OpenJDK 64-Bit Server VM). Done.
- **What a JDK is:** the Java Development Kit. It contains the JVM (the engine that runs Java programs) plus build tools. Synthea is a Java program shipped as a `.jar` file, and a `.jar` cannot run without Java, so we install it. It is only used for this data-generation step; it is unrelated to AWS and to our Python venv.

### Step 2 — Download Synthea

```powershell
Invoke-WebRequest -Uri "https://github.com/synthetichealth/synthea/releases/download/master-branch-latest/synthea-with-dependencies.jar" -OutFile "data_generation\synthea-with-dependencies.jar"
Get-Item data_generation\synthea-with-dependencies.jar | Select-Object Name, Length
```

- `Invoke-WebRequest` = PowerShell's download command. `-Uri` = source, `-OutFile` = where to save it.
- `Get-Item ... | Select-Object Name, Length` = confirm the file exists and show its size in bytes.
- **Result:** `synthea-with-dependencies.jar`, 197,026,434 bytes (~188 MB). Done.
- The `.jar` is gitignored (`*.jar`), so it is never pushed to GitHub. Anyone cloning the repo downloads it with the same command.

### Step 3 — Generate the data

```powershell
java -jar data_generation\synthea-with-dependencies.jar -p 300 -s 42 --exporter.years_of_history 5 Massachusetts
```

- `java -jar <file>` = run the Synthea program.
- `-p 300` = population size (300 living patients). `-s 42` = random seed: the same seed gives identical data, so the project is reproducible.
- `--exporter.years_of_history 5` = each patient's history is limited to 5 years (default is 10) to keep the size down.
- `Massachusetts` = the US state simulated (hospitals, doctors, addresses come from there).
- FHIR export is on by default; files go to `output/fhir/` (gitignored). `output/metadata/` holds run information.

**Result (measured):**

| Item                              | Value                                                            |
| --------------------------------- | ---------------------------------------------------------------- |
| Files in `output/fhir/`         | 343                                                              |
| Patient bundles                   | 341 (300 living + extra patients who died during the simulation) |
| `hospitalInformation*.json`     | 1 file, 1.56 MB (Organization/Location resources)                |
| `practitionerInformation*.json` | 1 file, 1.64 MB (Practitioner/PractitionerRole resources)        |
| Typical patient file              | 1–1.7 MB                                                        |
| **Total size**              | **689 MB**                                                 |

**Deviation from the plan:** the plan targets raw data < 500 MB; we got 689 MB.

> **Units note:** PowerShell reported 689 MB because it divides by 1,048,576 (MiB). The Python scripts divide by 1,000,000 and report the same data as **722.6 MB**. Both are the same data; the docs use the decimal figure from here on.

**Decision: data size — keep.** We keep this data instead of regenerating with fewer patients:
- S3 storage for ~0.7 GB costs only a couple of cents per month.
- Split into 10 batches, each is 46–111 MB, small for a Glue job (2 workers, 10-minute timeout). If the Glue job turns out too slow, we reduce the data then.
- Fewer patients would leave even fewer inpatient stays (283 now) for the readmission KPI.
- Known limitation: the doctors/hospital files are not reproducible because `-cs` (clinician seed) was not set. If the data is ever regenerated, add `-cs 42`.

### The run metadata file (`output/metadata/*.json`)

A log of how the data was generated. It does **not** describe columns and the pipeline never reads it.

| Field                            .     | Value                              | Meaning                                                                                                                                               |
| ---------------------------------------------------- | ---------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| `runID`                                            | `7a0b183d-...`                   | Unique id of this run                                                                                                                                 |
| `seed`                                             | `42`                             | Starting number for the random generator for patients (see below)                                                                                     |
| `clinicianSeed`                                    | `1792681729651`                  | Same idea, but for doctors and hospitals. We did not set it, so Synthea used the current time in milliseconds.                                        |
| `referenceTime` / `endTime`                      | `20261022`                       | The simulated "today" (22 Oct 2026). With 5 years of history, patient records cover about Oct 2021 to Oct 2026.                                       |
| `version`                                          | `bd8a52b`                        | Git commit of the Synthea build that was used                                                                                                         |
| `patientCount`                                     | `341`                            | Patients written: 300 living (`-p 300`) + 41 who died during the simulation                                                                         |
| `providerCount`                                    | `1327`                           | Healthcare providers created for the simulated state (the hospital file we received lists 521 organizations; we have not investigated the difference) |
| `payerCount`                                       | `9`                              | Insurance companies in the simulation                                                                                                                 |
| `javaVersion`                                      | `17.0.20.1`                      | The Java that ran it                                                                                                                                  |
| `generate.thread_pool_size` / `generatorThreads` | `-1` / `8`                     | `-1` = "choose automatically"; it chose 8 parallel threads                                                                                          |
| `runStartTime` / `runTimeInSeconds`              | `2026-10-22T15:08:49Z` / `102` | When it started and how long it took                                                                                                                  |
| `exporter.years_of_history`                        | `5`                              | Our setting                                                                                                                                           |
| `state`                                            | `Massachusetts`                  | Our setting                                                                                                                                           |
| `modules`                                          | `*`                              | All of Synthea's disease/care rules were used                                                                                                         |

**What the seed is, and why 42:** computers produce "random" numbers from a formula that starts at a _seed_ number. The same seed gives the same sequence, so the same patients every time. This makes the project **reproducible**: a teammate running our command gets the same patients. The value 42 is arbitrary (a programmer's tradition from _The Hitchhiker's Guide to the Galaxy_); any number would work. Different seed = different patients.

**Reproducibility gap found:** `-s 42` fixes the patients only. The doctors/hospitals use `clinicianSeed`, which was time-based, so a re-run would give _different_ doctors and hospital files (the number in the file names `hospitalInformation1792681729651.json` is that same value). Fix `[EXTRA]`: add `-cs 42` to the command. We will do this if we regenerate; if we keep the current data it only matters for anyone rerunning the project.

## Where the data lives during this phase

Everything is **local** in this step: jar in `data_generation/`, generated files in `output/`, batches in `data/landing/`. Nothing is in S3 until the upload to S3 (`aws s3 sync data/landing s3://<bucket>/landing/`).

### Step 4 — Split the files into 10 daily batches
```powershell
python data_generation\split_batches.py
```
**Why:** in real life data arrives daily. We simulate that: batch N is "the data that arrives on day N" (`ingest_date = 2026-11-01 + (N-1) days`, applied by the ingest Lambda). The Lambda will copy one batch per run.

**What `data_generation/split_batches.py` does:**
1. Lists the JSON files in `output/fhir/` and separates the 2 shared files (`hospitalInformation*`, `practitionerInformation*`) from the 341 patient files.
2. Sorts the patient files and deals them out round-robin (file 1 → batch 1, file 2 → batch 2, ... file 11 → batch 1 again). Sorting first makes the result identical on every run.
3. Puts the two shared files into batch 1 (reference data must exist before the patient data that points to it).
4. **Copies** (never moves) the files into `data/landing/batch_01 ... batch_10`.
5. Prints a summary and checks that every source file landed exactly once.
- Fails early with a clear message if: fewer patient files than batches, the shared files are missing, or `data/landing` already has batches (re-run with `--overwrite` to replace them).

**Result:**
| batch | files | MB |
|---|---|---|
| batch_01 | 37 (35 patients + 2 shared files; see note) | 110.8 |
| batch_02 | 34 | 45.8 |
| batch_03 | 34 | 67.1 |
| batch_04 | 34 | 71.3 |
| batch_05 | 34 | 59.9 |
| batch_06 | 34 | 94.1 |
| batch_07 | 34 | 77.6 |
| batch_08 | 34 | 68.0 |
| batch_09 | 34 | 56.5 |
| batch_10 | 34 | 71.7 |
| **total** | **343** | **722.6** |

Note on batch_01: 341 patients / 10 batches = 34 each plus 1 left over (goes to batch_01) = 35 patient files, plus 2 shared files = 37. Batch sizes differ (46–111 MB) because patients differ in history length; the count per batch is balanced, not the size.
A second run without `--overwrite` was refused with exit code 1, as designed.
`data/` is gitignored, so batches are never pushed to GitHub; anyone can recreate them with the Synthea command + this script.

### Step 5 — Exploration notebook
`notebooks/source_data_analysis.ipynb` answers, with code and charts: what files exist, what a bundle looks like, which resource types exist, which encounter classes exist, how references work, whether the silver columns can be filled, whether keys join, and whether the batches are correct. It is saved **with its outputs**, so it can be read on GitHub without running it.

Key results from the notebook:
- Joins: encounter→patient 100%, encounter→organization 100%, encounter→practitioner **by NPI 100% / by uuid 0%**, condition→encounter 100%.
- No encounter ends before it starts; no duplicate encounter ids.
- Inpatient (`IMP`) length of stay: 283 stays, minimum 24 h, median ≈ 104 h (≈ 4.3 days), maximum ≈ 2,280 h (≈ 95 days).
- The `resolve_ref` helper (turns both reference styles into plain ids) is tested with asserts here and will move to `glue_jobs/fhir_utils.py` when the Glue job is built.

**How to run it** (VS Code: open the notebook, choose the `.venv` kernel top-right, *Run All*). From a terminal:
```powershell
python -m nbconvert --to notebook --execute --inplace notebooks\source_data_analysis.ipynb
```
**Gotcha we hit (and what it really was):** when the notebook was executed with the venv's Python *without activating the venv*, `python -m jupyter nbconvert ...` failed with `No module named 'jupyter_client'`. The `jupyter` launcher searches `PATH` for a `jupyter-nbconvert` program, and with the venv inactive the first match belonged to a *different* Python installation that lacks our packages. This is **not** a Jupyter problem: with the venv activated (`(.venv)` in the prompt), `.venv\Scripts` is first on `PATH` and `jupyter nbconvert` works. `python -m nbconvert` is a safer habit because it always uses the Python you call it with.

## Done when
- Batches exist (`data/landing/batch_01..10`). ✅
- Total size known (722.6 MB). ✅
- Field paths for every silver column documented in the notebook and in `data_dictionary.md`. ✅

## Housekeeping
- The `.gitkeep` placeholders in `data_generation/` and `notebooks/` were deleted because those folders now hold real files.
