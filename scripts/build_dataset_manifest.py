# -*- coding: utf-8 -*-
"""Regenerate data/dataset_manifest.csv — the flat, human-readable provenance
index of every document in the dataset.

Source of truth is data/memory/manifest.json for document identity/provenance
(doc_id, source, doc_type, file_path), and data/memory/_ground_truth/*.json for
per-document rendering tier and case link.

Ground truth wins on `rendering` deliberately: batch1_render.py assigns the
tier at render time and writes it into the ground-truth JSON, but
manifest.json was written from the pre-render plan and never updated — 21 of
its entries disagreed with what was actually rendered. This script re-syncs
manifest.json from ground truth as its first step, so the two stop drifting.

The previous hand-maintained CSV covered only the original 40-document
Batch-0 and went stale once Batch-1 (79 more documents) landed.

Columns:
  filename          basename of the PDF
  source_type       synthetic | scraped
  category          document type (FIR, Case Diary, Witness Statement, ...)
  date_added        2026-07-16 Batch-0, 2026-07-20 Batch-1 (verified via mtime
                    of files untouched since generation)
  status            ok | quarantined: <reason>
  rendering         clean | handwritten | (blank for Batch-0, which predates
                    the rendering-tier concept and is all clean-style)
  case_id           owning Case, where one exists (Batch-1 + retrofitted)
  in_ingestion_set  yes if present in data/documents/ (the directory
                    config.DOCUMENTS_DIR / ingest_directory() actually reads).
                    Handwritten-tier documents are deliberately excluded, per
                    the post-POC OCR deferral.
"""
import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MEMORY = ROOT / "data" / "memory"
GT_DIR = MEMORY / "_ground_truth"
DOCS_DIR = ROOT / "data" / "documents"
OUT = ROOT / "data" / "dataset_manifest.csv"

FIELDS = ["filename", "source_type", "category", "date_added", "status",
          "rendering", "case_id", "in_ingestion_set"]

# Batch-0 predates the ground-truth/rendering-tier design; its date is the one
# already recorded in the previous manifest. Batch-1's date is the verified
# mtime of files untouched since generation.
BATCH0_DATE = "2026-07-16"
BATCH1_DATE = "2026-07-20"

# Excluded from ingestion with a documented root cause — see
# data/quarantine/README.md (Docling table-structure misdetection).
QUARANTINED = {
    "FIR-2026-CYBER-002.pdf": "quarantined: Docling table misdetection, see data/quarantine/README.md",
}


def _sync_manifest_from_ground_truth(manifest: list, gt: dict) -> int:
    """Re-point manifest.json's rendering/case_id at the ground truth, which is
    what the renderer actually wrote. Returns the number of entries corrected."""
    fixed = 0
    for entry in manifest:
        truth = gt.get(entry.get("doc_id", ""))
        if not truth:
            continue
        for field in ("rendering", "case_id"):
            if field in truth and entry.get(field) != truth[field]:
                entry[field] = truth[field]
                fixed += 1
    if fixed:
        (MEMORY / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return fixed


def main():
    manifest = json.loads((MEMORY / "manifest.json").read_text(encoding="utf-8"))
    gt = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in GT_DIR.glob("*.json")}
    ingested = {p.name for p in DOCS_DIR.glob("*.pdf")}

    corrected = _sync_manifest_from_ground_truth(manifest, gt)
    if corrected:
        print(f"re-synced {corrected} stale field(s) in manifest.json from ground truth")

    rows = []
    for entry in manifest:
        file_path = entry.get("file_path", "")
        if not file_path:
            continue
        filename = Path(file_path).name
        doc_id = entry.get("doc_id", "")

        # Batch-1 documents are exactly those with a ground-truth record.
        is_batch1 = doc_id in gt

        rows.append({
            "filename": filename,
            "source_type": entry.get("source", ""),
            "category": entry.get("doc_type", ""),
            "date_added": BATCH1_DATE if is_batch1 else BATCH0_DATE,
            "status": QUARANTINED.get(filename, "ok"),
            "rendering": entry.get("rendering", ""),
            "case_id": entry.get("case_id", ""),
            "in_ingestion_set": "yes" if filename in ingested else "no",
        })

    rows.sort(key=lambda r: r["filename"])

    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    ingested_count = sum(1 for r in rows if r["in_ingestion_set"] == "yes")
    print(f"wrote {len(rows)} rows to {OUT}")
    print(f"  in ingestion set: {ingested_count}, excluded: {len(rows) - ingested_count}")


if __name__ == "__main__":
    main()
