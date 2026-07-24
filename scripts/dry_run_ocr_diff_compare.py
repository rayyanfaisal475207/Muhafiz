"""Dry-run: OCR-diff mechanism validation, comparing the two vision paths the
real ingestion pipeline already has wired up (src/ingestion/loaders/image_loader.py) —
local (ngrok) vision and Gemini 2.5 Flash vision — side by side against ground
truth. Groq has no vision-capable model in its current catalog (checked
against /openai/v1/models), so it's excluded rather than silently skipped.

PaddleOCR/Tesseract remain unavailable in this environment (see the dry-run
status report) — this script validates the ground-truth-diff mechanism end to
end using what IS available and already production-wired, not a replacement
for eventually re-testing against a real OCR engine.
"""
import base64
import json
import os
import sys
from pathlib import Path

import fitz  # PyMuPDF
import httpx
import jiwer
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import config  # noqa: E402

GT_DIR = ROOT / "data" / "memory" / "_ground_truth"
SCRATCH_DIR = ROOT / "data" / "memory" / "_dry_run_scratch"

TARGETS = [
    ("WITNESS-FIR-2026-ARMS-001-01", ROOT / "data/memory/witness_statements/WITNESS-FIR-2026-ARMS-001-01.pdf", "scanned"),
    ("WITNESS-FIR-2026-ARMS-001-02", ROOT / "data/memory/witness_statements/WITNESS-FIR-2026-ARMS-001-02.pdf", "handwritten"),
]

TRANSCRIBE_PROMPT_SUFFIX = (
    "\n\nThis is a scanned Urdu-language police document. Transcribe ALL visible text exactly, "
    "in Urdu script, top to bottom, left field values and the narrative paragraph included.\n"
    "STRICT OUTPUT FORMAT — violating any of these makes your answer unusable:\n"
    "- Output raw Urdu text ONLY. No markdown, no code fences, no tables, no bullet points, no headers.\n"
    "- No English translation. No English transliteration. No parenthetical notes of any kind.\n"
    "- Do not explain what the document is. Do not describe the image. Do not add a preamble or a summary.\n"
    "- Keep digits as digits (e.g. 2026-05-02) exactly as shown.\n"
    "- If a word is truly illegible, write [?] in its place and continue — do not stop or apologize.\n"
    "Your entire response must be nothing but the transcribed lines, in order, separated by newlines."
)


def call_local_vision_strict(image_bytes: bytes) -> str:
    """Same endpoint/auth as src/ingestion/loaders/image_loader.py's
    _call_local_vision, but with the strict transcription-only prompt —
    that function's prompt is hardcoded for general "describe everything"
    vision use, not OCR-transcription, and takes no prompt override."""
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    payload = {
        "model": config.LOCAL_LLM_MODEL,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            {"type": "text", "text": TRANSCRIBE_PROMPT_SUFFIX},
        ]}],
        "max_tokens": 800,
        "temperature": 0.0,
    }
    r = httpx.post(
        f"{config.LOCAL_LLM_URL.rstrip('/')}/v1/chat/completions",
        json=payload,
        headers={"Authorization": f"Bearer {config.LOCAL_LLM_API_KEY or 'local-key'}"},
        timeout=90,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


def call_gemini_vision_strict(image_bytes: bytes) -> str:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=config.GEMINI_API_KEY)
    response = client.models.generate_content(
        model=config.GEMINI_MODEL,
        contents=[
            types.Part.from_bytes(data=image_bytes, mime_type="image/png"),
            TRANSCRIBE_PROMPT_SUFFIX,
        ],
    )
    return (response.text or "").strip()


def pdf_to_png_bytes(pdf_path: Path) -> bytes:
    doc = fitz.open(str(pdf_path))
    pix = doc[0].get_pixmap()
    out_path = SCRATCH_DIR / f"{pdf_path.stem}_for_ocr_compare.png"
    pix.save(str(out_path))
    return out_path.read_bytes()


def ground_truth_text(doc_id: str) -> str:
    gt = json.loads((GT_DIR / f"{doc_id}.json").read_text(encoding="utf-8"))
    parts = list(gt["structured_fields"].values())
    narrative_key = "narrative_tehrir" if "narrative_tehrir" in gt else "narrative_statement"
    parts.append(gt[narrative_key])
    return " ".join(str(p) for p in parts)


def score(gt_text: str, ocr_text: str) -> dict:
    if not ocr_text.strip():
        return {"cer": 1.0, "wer": 1.0}
    return {"cer": jiwer.cer(gt_text, ocr_text), "wer": jiwer.wer(gt_text, ocr_text)}


def main():
    results = {}
    for doc_id, pdf_path, tier in TARGETS:
        img_bytes = pdf_to_png_bytes(pdf_path)
        gt_text = ground_truth_text(doc_id)

        entry = {"tier": tier, "ground_truth_text": gt_text, "engines": {}}

        # Local (ngrok) vision — same endpoint/auth as production, strict prompt
        try:
            local_text = call_local_vision_strict(img_bytes)
        except Exception as exc:
            local_text = ""
            entry["engines"]["local_llm"] = {"error": str(exc)}
        if local_text:
            entry["engines"]["local_llm"] = {"text": local_text, **score(gt_text, local_text)}

        # Gemini 2.5 Flash vision — same endpoint/auth as production, strict prompt
        try:
            gemini_text = call_gemini_vision_strict(img_bytes)
        except Exception as exc:
            gemini_text = ""
            entry["engines"]["gemini"] = {"error": str(exc)}
        if gemini_text:
            entry["engines"]["gemini"] = {"text": gemini_text, **score(gt_text, gemini_text)}

        results[doc_id] = entry

        print(f"\n=== {doc_id} ({tier}) ===")
        for engine, r in entry["engines"].items():
            if "error" in r:
                print(f"  {engine}: ERROR — {r['error']}")
            else:
                print(f"  {engine}: CER={r['cer']:.3f}  WER={r['wer']:.3f}")

    out_path = SCRATCH_DIR / "ocr_diff_compare_results.json"
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\nwrote", out_path)


if __name__ == "__main__":
    main()
