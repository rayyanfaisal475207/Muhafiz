# -*- coding: utf-8 -*-
"""
Module 151 — the verifier arm: KB questions end-to-end with ONE boundary
replaced, the relevance evaluator, so that every run reaches the Verifier.

WHY. On the days this module ran, the sampled relevance evaluator rejected
KB5/KB9's first-pass windows often enough that the Semantic Search deadline
(360 s, Module 67) stopped 7 of the 9 before-arm runs before any answer was
generated (§4.1 of the result file). That is upstream of the Verifier and
not this module's defect; but it means the end-to-end arms measure the
evaluator's coin flips, not the change. This runner is Module 82 §4c's
discipline applied one stage earlier: `rag.evaluate_relevance` returns
`relevant=True` on the first pass, and EVERYTHING downstream is real —
the real window (retrieval, reranker, data-half aggregate), the real
generator, the real `verify_grounding()` (flag off or on, from the
environment), the real ABSTAINED contract. Nothing in `src/` is modified.

The evaluator being bypassed means the window is whatever the first pass
retrieved — the same window the evaluator saw on the 2026-09-14 run, which
it accepted at attempt 1 (KB9) / attempt 2 (KB5).

    SCHEMA_ABSENCE_GROUNDING_ENABLED=false PYTHONPATH=. python -X utf8 \
        evaluation/module151_verifier_arm.py --questions KB9,KB5,KB2 --runs 3 --arm varm_before
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _patch_evaluator():
    import src.pipeline.harness.tools.rag as rag_mod

    async def _always_relevant(original_query, rewritten_query, retrieved_chunks, *a, **kw):
        return {
            "relevant": True,
            "reason": "MODULE 151 VERIFIER ARM: evaluator bypassed on this run (first-pass window kept).",
        }

    rag_mod.evaluate_relevance = _always_relevant


if __name__ == "__main__":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    _patch_evaluator()
    from evaluation import module151_inprocess_run as base
    base.main()
