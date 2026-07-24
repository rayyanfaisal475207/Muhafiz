# -*- coding: utf-8 -*-
"""Re-render every handwritten-tier Batch-1 document (produced by
batch1_render.py) with the fixed _shape_line, to eliminate the numeral/
letter-joining bugs the old whole-buffer HarfBuzz shaping had. Backs up
first. Skips WITNESS-FIR-2026-ARMS-001-01/02 — those were rendered via the
separate dry_run_render_docs.py script and their content (pure-digit or
pure-Urdu fields only, no digit+Urdu mixing in one line) doesn't trigger
the bug, so they're left untouched.
"""
import json
import shutil
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import batch1_render as br

ROOT = Path(__file__).resolve().parent.parent
GT_DIR = ROOT / "data" / "memory" / "_ground_truth"
BACKUP_DIR = ROOT / "data" / "_backup_pre_shape_line_fix"
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

SKIP = {"WITNESS-FIR-2026-ARMS-001-01", "WITNESS-FIR-2026-ARMS-001-02"}


def main():
    seed_counter = 9000
    done = []
    for gt_path in sorted(GT_DIR.glob("*.json")):
        doc = json.loads(gt_path.read_text(encoding="utf-8"))
        if doc.get("rendering") != "handwritten" or doc["doc_id"] in SKIP:
            continue

        folder = br.FOLDER_BY_TYPE.get(doc["doc_type"])
        if not folder:
            continue
        pdf_path = ROOT / "data" / "memory" / folder / f"{doc['doc_id']}.pdf"
        if not pdf_path.exists():
            continue

        shutil.copy2(pdf_path, BACKUP_DIR / pdf_path.name)

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
            print(f"  (minimal-degradation fallback for {doc['doc_id']})")
        br.wrap_pdf_from_image(img2, pdf_path)
        seed_counter += 1
        done.append(doc["doc_id"])
        print("re-rendered", doc["doc_id"])

    print(f"\nDone. {len(done)} documents re-rendered with the fixed shaping. Backups in {BACKUP_DIR}")


if __name__ == "__main__":
    main()
