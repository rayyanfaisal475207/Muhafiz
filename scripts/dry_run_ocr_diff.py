"""Dry-run: run PaddleOCR against the two noisy renders and diff the output
against ground truth to get a real CER/WER, per SYNTHETIC_DATASET_PLAN.md §5.2/§6.2.
"""
import json
from pathlib import Path

import fitz  # PyMuPDF
import numpy as np
import jiwer
from paddleocr import PaddleOCR

ROOT = Path(__file__).resolve().parent.parent
GT_DIR = ROOT / "data" / "memory" / "_ground_truth"
SCRATCH_DIR = ROOT / "data" / "memory" / "_dry_run_scratch"

TARGETS = [
    ("WITNESS-FIR-2026-ARMS-001-01", ROOT / "data/memory/witness_statements/WITNESS-FIR-2026-ARMS-001-01.pdf", "scanned"),
    ("WITNESS-FIR-2026-ARMS-001-02", ROOT / "data/memory/witness_statements/WITNESS-FIR-2026-ARMS-001-02.pdf", "handwritten"),
]


def pdf_to_image_array(pdf_path: Path) -> np.ndarray:
    doc = fitz.open(str(pdf_path))
    pix = doc[0].get_pixmap()
    img_path = SCRATCH_DIR / f"{pdf_path.stem}_for_ocr.png"
    pix.save(str(img_path))
    return str(img_path)


def ground_truth_text(doc_id: str) -> str:
    gt = json.loads((GT_DIR / f"{doc_id}.json").read_text(encoding="utf-8"))
    parts = list(gt["structured_fields"].values())
    narrative_key = "narrative_tehrir" if "narrative_tehrir" in gt else "narrative_statement"
    parts.append(gt[narrative_key])
    return " ".join(str(p) for p in parts)


def main():
    ocr = PaddleOCR(lang="ur", use_doc_orientation_classify=False,
                     use_doc_unwarping=False, use_textline_orientation=False)

    results = {}
    for doc_id, pdf_path, tier in TARGETS:
        img_path = pdf_to_image_array(pdf_path)
        ocr_result = ocr.predict(img_path)

        recognized_lines = []
        for res in ocr_result:
            texts = res.get("rec_texts") if isinstance(res, dict) else getattr(res, "rec_texts", None)
            if texts:
                recognized_lines.extend(texts)
        ocr_text = " ".join(recognized_lines)

        gt_text = ground_truth_text(doc_id)

        cer = jiwer.cer(gt_text, ocr_text) if ocr_text.strip() else 1.0
        wer = jiwer.wer(gt_text, ocr_text) if ocr_text.strip() else 1.0

        results[doc_id] = {
            "tier": tier,
            "ground_truth_text": gt_text,
            "ocr_text": ocr_text,
            "cer": cer,
            "wer": wer,
        }

        print(f"\n=== {doc_id} ({tier}) ===")
        print("CER:", round(cer, 3), " WER:", round(wer, 3))

    out_path = SCRATCH_DIR / "ocr_diff_results.json"
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\nWrote", out_path)


if __name__ == "__main__":
    main()
