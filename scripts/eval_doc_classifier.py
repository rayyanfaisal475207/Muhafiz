"""
Validate src/extraction/doc_classifier.py against the labeled ground truth
(data/memory/_ground_truth/*.json), per Phase 4.4's own requirement: report
accuracy and confusion pattern, not just "it seems to work."

    python scripts/eval_doc_classifier.py

Checks the model server (config.MODEL_SERVER_BASE_URL) first and exits
early with a clear message if it's unreachable, rather than firing ~76
calls at a dead ngrok tunnel one timeout at a time.
"""
import asyncio
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.extraction import doc_classifier
from src.extraction.llm_health import model_server_healthy

_GROUND_TRUTH_DIR = Path(__file__).resolve().parent.parent / "data" / "memory" / "_ground_truth"

# Keys that are metadata, not document content — excluded when building the
# text a real ingested document's extracted text would resemble.
_METADATA_KEYS = {
    "doc_id", "source", "case_id", "entities", "rendering", "language",
    "related_fir", "cnic_shown_in",
}


def _document_text(record: dict) -> str:
    """Approximate the extracted-text a real ingested document would produce,
    by concatenating every string/dict field's values in file order."""
    parts = []

    def _walk(value):
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, dict):
            for v in value.values():
                _walk(v)

    for key, value in record.items():
        if key in _METADATA_KEYS or key == "doc_type":
            continue
        _walk(value)
    return "\n".join(parts)


async def main() -> None:
    if not await model_server_healthy():
        print(f"Model server unreachable — check MODEL_SERVER_BASE_URL. Aborting.")
        sys.exit(1)

    records = []
    for path in sorted(_GROUND_TRUTH_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if "doc_type" not in data:
            continue
        records.append((path.name, data))

    print(f"Evaluating doc_classifier on {len(records)} labeled documents...\n")

    confusion: dict[str, Counter] = defaultdict(Counter)
    correct = 0
    failed = []

    for name, record in records:
        text = _document_text(record)
        expected = record["doc_type"]
        result = await doc_classifier.classify_document(text)
        if result is None:
            failed.append(name)
            confusion[expected]["<extraction failed>"] += 1
            continue
        predicted = result["doc_type"]
        confusion[expected][predicted] += 1
        if predicted == expected:
            correct += 1

    total = len(records)
    print(f"Accuracy: {correct}/{total} ({100 * correct / total:.1f}%)\n")

    if failed:
        print(f"Extraction failed (no usable LLM response) on {len(failed)} docs: {failed}\n")

    print("Confusion matrix (rows = true doc_type, columns = predicted):")
    all_predicted = sorted({p for row in confusion.values() for p in row})
    header = "true \\ predicted".ljust(24) + "".join(p[:12].ljust(14) for p in all_predicted)
    print(header)
    for true_type in sorted(confusion):
        row = confusion[true_type]
        line = true_type.ljust(24) + "".join(str(row.get(p, 0)).ljust(14) for p in all_predicted)
        print(line)

    print("\nMisclassifications:")
    any_wrong = False
    for true_type, row in confusion.items():
        for predicted, count in row.items():
            if predicted != true_type:
                any_wrong = True
                print(f"  {true_type} -> {predicted}: {count}")
    if not any_wrong:
        print("  (none)")


if __name__ == "__main__":
    asyncio.run(main())
