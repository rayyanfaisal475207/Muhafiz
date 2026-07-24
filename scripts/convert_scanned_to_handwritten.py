# -*- coding: utf-8 -*-
"""One-off conversion: flip every remaining 'scanned'-tagged Batch-1 document
to 'handwritten' (Nastaliq raster + handwritten-severity Augraphy degradation,
matching batch1_render.py's handwritten tier), consolidating the deferred-OCR
test bucket into one rendering style instead of two. Backs up the old PDF +
ground truth first, since data/ is untracked by git.

Bypasses batch1_render.py's main()/assign_tier() deliberately: tier
assignment there is deterministic per doc_id, so simply flipping the ground
truth tag and re-running main() would just reassign the original "scanned"
tier again.
"""
import json
import shutil
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import batch1_render as br

ROOT = Path(__file__).resolve().parent.parent
GT_DIR = ROOT / "data" / "memory" / "_ground_truth"
BACKUP_DIR = ROOT / "data" / "_backup_pre_scanned_to_handwritten"
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

FOLDER_BY_TYPE = br.FOLDER_BY_TYPE

DOC_IDS = [
    "CHARGESHEET-FIR-2026-BUR-009",
    "FIR-2026-HAR-017",
    "FIR-2026-ARMS-003",
    "CHARGESHEET-FIR-2026-CYBER-006",
    "CASEDIARY-FIR-2026-FRAUD-015-01",
    "CASEDIARY-FIR-2026-CYBER-004-01",
    "WITNESS-FIR-2026-ARMS-003-01",
    "FIR-2026-THEFT-012",
    "FIR-2026-THEFT-010",
    "FIR-2026-DOM-013",
    "FIR-2026-CYBER-004",
    "DARKHAST-FIR-2026-THEFT-012",
    "DARKHAST-FIR-2026-FRAUD-016",
    "CASEDIARY-FIR-2026-ARMS-003-01",
]


def main():
    seed_counter = 5000
    for doc_id in DOC_IDS:
        gt_path = GT_DIR / f"{doc_id}.json"
        doc = json.loads(gt_path.read_text(encoding="utf-8"))
        assert doc["rendering"] == "scanned", f"{doc_id} is not tagged scanned: {doc['rendering']}"

        folder = FOLDER_BY_TYPE[doc["doc_type"]]
        pdf_path = ROOT / "data" / "memory" / folder / f"{doc_id}.pdf"

        shutil.copy2(pdf_path, BACKUP_DIR / f"{doc_id}.pdf")
        shutil.copy2(gt_path, BACKUP_DIR / f"{doc_id}.json")

        doc["rendering"] = "handwritten"
        gt_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")

        img = br._hb_raster(doc, br.NASTALIQ, font_size=36)
        img2 = None
        for retry_seed in (seed_counter, seed_counter + 1000, seed_counter + 2000):
            try:
                img2 = br.degrade(img, retry_seed, "handwritten")
                break
            except Exception:
                continue
        if img2 is None:
            img2 = br.degrade_minimal_fallback(img, seed_counter)
            print(f"  (used minimal-degradation fallback for {doc_id})")
        br.wrap_pdf_from_image(img2, pdf_path)
        seed_counter += 1
        print(f"converted {doc_id} -> handwritten")

    print(f"\nDone. {len(DOC_IDS)} documents converted. Backups in {BACKUP_DIR}")


if __name__ == "__main__":
    main()
