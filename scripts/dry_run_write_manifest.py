"""Dry-run: append manifest.json entries for the 3 dry-run documents, per
SYNTHETIC_DATASET_PLAN.md §5.4 (existing schema + language/rendering/entities/case_id).
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GT_DIR = ROOT / "data" / "memory" / "_ground_truth"
MANIFEST_PATH = ROOT / "data" / "memory" / "manifest.json"

DOC_FILE_PATHS = {
    "FIR-2026-ARMS-001": "firs/FIR-2026-ARMS-001.pdf",
    "WITNESS-FIR-2026-ARMS-001-01": "witness_statements/WITNESS-FIR-2026-ARMS-001-01.pdf",
    "WITNESS-FIR-2026-ARMS-001-02": "witness_statements/WITNESS-FIR-2026-ARMS-001-02.pdf",
}


def build_entry(gt: dict) -> dict:
    entry = {
        "doc_id": gt["doc_id"],
        "doc_type": gt["doc_type"],
        "source": gt["source"],
        "police_station": gt["police_station"],
        "date_registered": gt["date_registered"],
        "file_path": DOC_FILE_PATHS[gt["doc_id"]],
        # New fields per §5.4
        "language": gt["language"],
        "rendering": gt["rendering"],
        "entities": gt["entities"],
        "case_id": gt["case_id"],
    }
    if gt["doc_type"] == "FIR":
        entry["category"] = gt["category"]
        entry["sections"] = gt["sections"]
    else:
        entry["related_fir"] = gt.get("related_fir")
    return entry


def main():
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    existing_ids = {e["doc_id"] for e in manifest}

    added = 0
    for gt_path in sorted(GT_DIR.glob("*.json")):
        gt = json.loads(gt_path.read_text(encoding="utf-8"))
        if gt["doc_id"] in existing_ids:
            print("skip (already present):", gt["doc_id"])
            continue
        manifest.append(build_entry(gt))
        added += 1
        print("added manifest entry:", gt["doc_id"])

    MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nWrote {added} new entries. manifest.json now has {len(manifest)} total documents.")


if __name__ == "__main__":
    main()
