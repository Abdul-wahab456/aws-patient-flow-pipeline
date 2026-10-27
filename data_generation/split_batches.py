"""Split the Synthea FHIR output into daily batches.

Simulates a daily feed: each batch is the data that "arrives" on one day.

    output/fhir/                      data/landing/
      <patient>.json  (341)    -->      batch_01/  hospitalInformation*.json
      hospitalInformation*.json         batch_01/  practitionerInformation*.json
      practitionerInformation*.json     batch_01 ... batch_10/  (patient files)

Rules
- Patient files are shared out round-robin over the batches, so batches have
  a similar size. The result is deterministic (files are sorted first).
- The hospital and practitioner reference files go into batch_01 only.
- Source files are copied, never moved or changed.

Usage (from the project root):
    python data_generation/split_batches.py
    python data_generation/split_batches.py --overwrite
"""
import argparse
import shutil
import sys
from pathlib import Path

REFERENCE_PREFIXES = ("hospitalInformation", "practitionerInformation")


def split_files(source: Path):
    """Return (patient_files, reference_files) found in the source folder, sorted."""
    all_files = sorted(source.glob("*.json"))
    reference_files = [f for f in all_files if f.name.startswith(REFERENCE_PREFIXES)]
    patient_files = [f for f in all_files if f not in reference_files]
    return patient_files, reference_files


def assign_to_batches(patient_files, n_batches):
    """Round-robin: file i goes to batch (i % n_batches)."""
    batches = [[] for _ in range(n_batches)]
    for i, f in enumerate(patient_files):
        batches[i % n_batches].append(f)       # file 0 -> batch 1, file 1 -> batch 2, ..., then round again
    return batches


def prepare_destination(dest: Path, overwrite: bool):
    """Create the landing folder; refuse to mix with batches from an earlier run."""
    existing = list(dest.glob("batch_*")) if dest.exists() else []
    if existing and not overwrite:
        sys.exit(f"{dest} already has {len(existing)} batch folders. "
                 "Re-run with --overwrite to replace them.")
    for folder in existing:
        shutil.rmtree(folder)
    dest.mkdir(parents=True, exist_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", default="output/fhir", type=Path)
    parser.add_argument("--dest", default="data/landing", type=Path)
    parser.add_argument("--batches", default=10, type=int)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    patient_files, reference_files = split_files(args.source)

    # Fail early with a clear message instead of producing empty or partial batches.
    if len(patient_files) < args.batches:
        sys.exit(f"Found {len(patient_files)} patient files in {args.source}; "
                 f"need at least {args.batches}. Did the Synthea run finish?")
    if len(reference_files) != 2:
        sys.exit(f"Expected 2 reference files (hospital + practitioner) in "
                 f"{args.source}, found {len(reference_files)}.")

    prepare_destination(args.dest, args.overwrite)

    batches = assign_to_batches(patient_files, args.batches)
    batches[0] = reference_files + batches[0]  # reference data arrives with batch 1

    print(f"{'batch':<10}{'files':>7}{'MB':>9}")
    total_files = total_bytes = 0
    for number, files in enumerate(batches, start=1):
        batch_dir = args.dest / f"batch_{number:02d}"
        batch_dir.mkdir()
        for f in files:
            shutil.copy2(f, batch_dir / f.name)
        size = sum(f.stat().st_size for f in files)
        total_files += len(files)
        total_bytes += size
        print(f"{batch_dir.name:<10}{len(files):>7}{size / 1e6:>9.1f}")

    print(f"{'total':<10}{total_files:>7}{total_bytes / 1e6:>9.1f}")

    # Every source file must land in exactly one batch.
    source_count = len(patient_files) + len(reference_files)
    if total_files != source_count:
        sys.exit(f"Copied {total_files} files but the source has {source_count}.")


if __name__ == "__main__":
    main()
