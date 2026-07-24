"""Dry-run: Tier-1 structural validation per SYNTHETIC_DATASET_PLAN.md §4.3 / §6.3.

Checks, for every dry-run ground-truth document:
  - required fields present for its doc_type
  - every referenced entity_id resolves against entity_roster.csv
  - text fields are valid, non-empty Urdu (basic script-range sanity check)

This must pass 100% before anything downstream (rendering, OCR, manifest)
is trusted, per the plan's hard-gate rule.
"""
import csv
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GT_DIR = ROOT / "data" / "memory" / "_ground_truth"
ROSTER_PATH = ROOT / "data" / "memory" / "entity_roster.csv"

REQUIRED_FIELDS = {
    "FIR": ["fir_number", "police_station", "date_time", "complainant_name",
            "accused_name", "sections"],
    "Witness Statement": ["fir_reference", "witness_name", "date_time_recorded",
                           "recording_officer"],
}

URDU_RANGE = re.compile(r"[؀-ۿݐ-ݿ]")


def load_entity_ids() -> set[str]:
    ids = set()
    with open(ROSTER_PATH, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ids.add(row["entity_id"])
    return ids


def validate_doc(path: Path, known_entities: set[str]) -> list[str]:
    errors = []
    doc = json.loads(path.read_text(encoding="utf-8"))

    doc_type = doc.get("doc_type")
    required = REQUIRED_FIELDS.get(doc_type)
    if required is None:
        errors.append(f"unknown doc_type '{doc_type}', no required-field spec to check against")
    else:
        fields = doc.get("structured_fields", {})
        for f in required:
            if not fields.get(f):
                errors.append(f"missing/empty required structured field '{f}'")

    for eid in doc.get("entities", []):
        if eid not in known_entities:
            errors.append(f"entity_id '{eid}' referenced but not found in entity_roster.csv")

    narrative_key = "narrative_tehrir" if "narrative_tehrir" in doc else "narrative_statement"
    narrative = doc.get(narrative_key, "")
    if not narrative.strip():
        errors.append(f"narrative field '{narrative_key}' is empty")
    elif not URDU_RANGE.search(narrative):
        errors.append(f"narrative field '{narrative_key}' contains no Urdu-range characters — possible encoding/generation failure")

    for k in ("doc_id", "language", "rendering", "case_id"):
        if not doc.get(k):
            errors.append(f"missing top-level field '{k}' required by manifest.json extension schema (§5.4)")

    return errors


def main():
    known_entities = load_entity_ids()
    all_ok = True
    for path in sorted(GT_DIR.glob("*.json")):
        errors = validate_doc(path, known_entities)
        status = "PASS" if not errors else "FAIL"
        print(f"[{status}] {path.name}")
        for e in errors:
            print(f"    - {e}")
        if errors:
            all_ok = False

    print()
    print("TIER-1 RESULT:", "PASS (100%)" if all_ok else "FAIL — fix before proceeding")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
