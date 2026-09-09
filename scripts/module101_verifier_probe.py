"""
Module 101 — verifier-input probe backend.

Starts the ORDINARY backend with exactly one thing added: a transparent
wrapper around `semantic_search.verify_grounding` that records, for every
RAG request that reaches the grounding gate, the complete input the
verifier was given (every chunk id, its `source_tool`, whether it carries
Module 61's `exhaustive_scope` marker, its full text) and the verdict it
returned (`grounded`, `off_topic`, `unsupported_claims`, `reason`).

NOTHING in `src/` is modified and NOTHING is changed about the verdict —
the wrapper awaits the real `verify_grounding()` and returns its result
unaltered. It exists because the brief's Phase 1 asks for the verifier's
own input and verdict on a failing and a passing run of the same question,
and neither the SSE stream nor `backend.log` carries the chunk corpus or
the itemised `unsupported_claims`.

Usage (from the worktree, PYTHONPATH=.):
    M101_PORT=8033 M101_DUMP=scratchpad/m101_verifier_io.jsonl \
    python scripts/module101_verifier_probe.py
"""
from __future__ import annotations

import json
import logging
import os
import time

import uvicorn

import src.pipeline.harness.agents.semantic_search as ss_mod
from src.pipeline.verifier import EXHAUSTIVE_SCOPE_META_KEY

logger = logging.getLogger("module101.probe")

_DUMP = os.environ.get("M101_DUMP", os.path.join("scratchpad", "m101_verifier_io.jsonl"))
_real_verify = ss_mod.verify_grounding


async def _recording_verify(*, answer, cited_chunks, case_id, **kwargs):
    result = await _real_verify(
        answer=answer, cited_chunks=cited_chunks, case_id=case_id, **kwargs
    )
    record = {
        "ts": time.time(),
        "case_id": case_id,
        "answer": answer,
        "answer_chars": len(answer or ""),
        "chunks": [
            {
                "n": i,
                "id": c.get("id"),
                "source_tool": (c.get("metadata") or {}).get("source_tool"),
                "source": (c.get("metadata") or {}).get("source")
                or (c.get("metadata") or {}).get("source_file"),
                "exhaustive_scope": bool(
                    (c.get("metadata") or {}).get(EXHAUSTIVE_SCOPE_META_KEY)
                ),
                "chars": len(c.get("text") or ""),
                "text": c.get("text") or "",
            }
            for i, c in enumerate(cited_chunks, start=1)
        ],
        "verdict": {
            "grounded": result.get("grounded"),
            "off_topic": result.get("off_topic"),
            "leaked_case_id": result.get("leaked_case_id"),
            "unsupported_claims": result.get("unsupported_claims"),
            "reason": result.get("reason"),
            "exhaustive_negative_override": result.get("exhaustive_negative_override"),
        },
    }
    os.makedirs(os.path.dirname(_DUMP) or ".", exist_ok=True)
    with open(_DUMP, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    logger.warning(
        "Module 101 PROBE: recorded verifier I/O — grounded=%s chunks=%d",
        result.get("grounded"), len(cited_chunks),
    )
    return result


ss_mod.verify_grounding = _recording_verify  # noqa: E305 — the point of this file.


if __name__ == "__main__":
    uvicorn.run(
        "src.main:app",
        host="127.0.0.1",
        port=int(os.environ.get("M101_PORT", "8033")),
        log_level="info",
    )
