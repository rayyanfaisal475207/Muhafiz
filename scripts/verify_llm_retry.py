"""
Verifies the empty-response retry in src/llm/client.py::_call_local.

Side A  — raw call to the model server at a budget too small for
          qwen3:14b's thinking trace. Expect an empty `response`.
Side B  — the same budget through _call_local, which now retries once
          locally with a doubled budget. Expect real content.

Run:  ./.venv/Scripts/python.exe verify_llm_retry.py
"""
import asyncio
import logging
import os
import sys

# Running this file directly puts scripts/ on sys.path, not the repo root,
# so `from src import ...` fails. Match the other scripts in this folder.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import httpx

from src import config
from src.llm.client import _call_local

logging.basicConfig(level=logging.WARNING, format="  [log] %(message)s")

BUDGET = 300
SYSTEM = "You are a strict evaluator. Think carefully step by step, then output JSON."
PROMPT = (
    "Analyse in depth whether these documents answer the question, considering "
    'every nuance, then output {"relevant":bool,"reason":str}. Question: describe '
    "FIR 201/26 narrative and weapon. Documents: a zimni report and a chalaan "
    "property list mentioning a 30 bore pistol."
)


async def main() -> int:
    print(f"model server : {config.LOCAL_LLM_URL}")
    print(f"model        : {config.LOCAL_LLM_MODEL}")
    print(f"budget       : {BUDGET} tokens\n")

    print("A. raw endpoint (no retry) — reproduces the bug")
    async with httpx.AsyncClient(timeout=config.LOCAL_LLM_TIMEOUT) as client:
        r = await client.post(
            config.LOCAL_LLM_URL,
            json={
                "system": SYSTEM,
                "prompt": PROMPT,
                "model": config.LOCAL_LLM_MODEL,
                "temperature": 0.1,
                "max_tokens": BUDGET,
            },
        )
        r.raise_for_status()
        body = r.json()
    raw = body.get("response") or ""
    think = body.get("thinking") or ""
    print(f"   http           : {r.status_code}")
    print(f"   tokens used    : {body.get('eval_count')} of {BUDGET}")
    print(f"   thinking chars : {len(think)}")
    print(f"   answer chars   : {len(raw)}")
    print(f"   -> {'EMPTY (bug reproduced)' if not raw.strip() else 'got content this time'}\n")

    print("B. through _call_local — the fix")
    try:
        fixed = await _call_local(SYSTEM, PROMPT, 0.1, BUDGET, "reasoning")
    except Exception as exc:
        print(f"   -> STILL FAILED: {type(exc).__name__}: {exc}")
        return 1
    print(f"   answer chars   : {len(fixed)}")
    print(f"   answer         : {fixed.strip()[:120]}")
    print("   -> RECOVERED\n")

    if not raw.strip():
        print("RESULT: bug reproduced on the raw call, fix recovered it. PASS")
    else:
        print(
            "RESULT: the raw call happened to answer at this budget, so the bug\n"
            "        did not reproduce this run (the trace length varies). The\n"
            "        fix path still returned content. Re-run to try again."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
