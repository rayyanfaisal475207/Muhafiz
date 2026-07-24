# -*- coding: utf-8 -*-
"""Sync every 'clean'-tier document into data/documents/ — the directory
config.DOCUMENTS_DIR / ingest_directory() actually reads. data/memory/ holds
the full generated corpus (including scanned/handwritten tiers, which stay
excluded from POC ingestion per the OCR-deferral decision, and non-document
files like case_index.csv / entity_roster.csv / _ground_truth/ / _fonts/).

Batch-0 documents (no _ground_truth entry, always clean-style) are already
present in data/documents/ and untouched here. This only adds/refreshes
Batch-1's clean-tier documents.
"""
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GT_DIR = ROOT / "data" / "memory" / "_ground_truth"
DOCS_DIR = ROOT / "data" / "documents"

FOLDER_BY_TYPE = {
    "FIR": "firs", "Witness Statement": "witness_statements", "Case Diary": "case_diaries",
    "Recovery Memo": "recovery_memos", "Complaint Application": "complaint_applications",
    "Charge Sheet": "charge_sheets", "Missing Person Report": "missing_persons",
}


def main():
    copied, skipped_nonclean, missing = [], [], []
    for gt_path in sorted(GT_DIR.glob("*.json")):
        doc = json.loads(gt_path.read_text(encoding="utf-8"))
        if doc.get("rendering") != "clean":
            skipped_nonclean.append(doc["doc_id"])
            continue
        folder = FOLDER_BY_TYPE.get(doc["doc_type"])
        if not folder:
            continue
        src = ROOT / "data" / "memory" / folder / f"{doc['doc_id']}.pdf"
        if not src.exists():
            missing.append(doc["doc_id"])
            continue
        dst = DOCS_DIR / src.name
        shutil.copy2(src, dst)
        copied.append(doc["doc_id"])

    print(f"Copied {len(copied)} clean-tier documents into {DOCS_DIR}")
    print(f"Excluded (scanned/handwritten, stay out of POC ingestion): {len(skipped_nonclean)}")
    if missing:
        print(f"WARNING: {len(missing)} clean-tier ground-truth entries have no rendered PDF: {missing}")


if __name__ == "__main__":
    main()
