"""Dry-run: OCR-diff mechanism validation using the local (ngrok-tunneled)
vision-capable LLM endpoint as a stand-in OCR engine, since PaddleOCR crashes
natively on this machine (see the dry-run status report).

IMPORTANT CAVEAT: this uses a small (2B) general-purpose vision-language model
as an OCR substitute, not a purpose-built OCR engine. It validates that the
ground-truth-diff mechanism (§5.2/§6.2) works end to end; the resulting
CER/WER numbers reflect this specific small model's Urdu-vision competence,
not what PaddleOCR/Tesseract would produce on the same images. Do not treat
these numbers as the "real" OCR error rate for the plausible-band check —
that still needs a real OCR engine once the PaddleOCR environment issue is
resolved.
"""
import base64
import json
import os
from pathlib import Path

import fitz  # PyMuPDF
import httpx
import jiwer
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
GT_DIR = ROOT / "data" / "memory" / "_ground_truth"
SCRATCH_DIR = ROOT / "data" / "memory" / "_dry_run_scratch"

TARGETS = [
    ("WITNESS-FIR-2026-ARMS-001-01", ROOT / "data/memory/witness_statements/WITNESS-FIR-2026-ARMS-001-01.pdf", "scanned"),
    ("WITNESS-FIR-2026-ARMS-001-02", ROOT / "data/memory/witness_statements/WITNESS-FIR-2026-ARMS-001-02.pdf", "handwritten"),
]

OCR_PROMPT = (
    "This is a scanned Urdu-language police document. Transcribe ALL visible text exactly as it "
    "appears, top to bottom. Output ONLY the raw transcription in Urdu script — no notes, no "
    "commentary, no markdown, no translation, no headers. If a word is illegible, write [?] in its place."
)


def pdf_to_png(pdf_path: Path) -> Path:
    doc = fitz.open(str(pdf_path))
    pix = doc[0].get_pixmap()
    out_path = SCRATCH_DIR / f"{pdf_path.stem}_for_llm_ocr.png"
    pix.save(str(out_path))
    return out_path


def call_vision_llm(image_path: Path) -> str:
    base = os.environ["LOCAL_LLM_URL"].rstrip("/")
    key = os.environ.get("LOCAL_LLM_API_KEY", "")
    b64 = base64.b64encode(image_path.read_bytes()).decode()

    payload = {
        "model": "qwen3.5-2b",
        "messages": [
            {"role": "user", "content": [
                {"type": "text", "text": OCR_PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ]}
        ],
        "max_tokens": 600,
        "temperature": 0.0,
    }
    r = httpx.post(f"{base}/v1/chat/completions", json=payload,
                    headers={"Authorization": f"Bearer {key}"}, timeout=90)
    r.raise_for_status()
    data = r.json()
    return data["choices"][0]["message"]["content"].strip()


def ground_truth_text(doc_id: str) -> str:
    gt = json.loads((GT_DIR / f"{doc_id}.json").read_text(encoding="utf-8"))
    parts = list(gt["structured_fields"].values())
    narrative_key = "narrative_tehrir" if "narrative_tehrir" in gt else "narrative_statement"
    parts.append(gt[narrative_key])
    return " ".join(str(p) for p in parts)


def main():
    results = {}
    for doc_id, pdf_path, tier in TARGETS:
        img_path = pdf_to_png(pdf_path)
        ocr_text = call_vision_llm(img_path)
        gt_text = ground_truth_text(doc_id)

        cer = jiwer.cer(gt_text, ocr_text)
        wer = jiwer.wer(gt_text, ocr_text)

        results[doc_id] = {
            "tier": tier,
            "ground_truth_text": gt_text,
            "llm_ocr_text": ocr_text,
            "cer": cer,
            "wer": wer,
        }
        print(f"{doc_id} ({tier}): CER={cer:.3f}  WER={wer:.3f}")

    out_path = SCRATCH_DIR / "ocr_diff_results_local_llm.json"
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print("wrote", out_path)


if __name__ == "__main__":
    main()
