# -*- coding: utf-8 -*-
"""
Module 101 — old-vs-new `verify_grounding()` equality control.

The live regression arm can only sample. This one is exhaustive over the
decision surface the change touches: it loads `origin/main`'s `verifier.py`
side by side with this branch's, drives BOTH with the same stubbed judge over
a matrix of (answer x chunk-window) inputs, and asserts the two return the
SAME dict everywhere except the cells the module intends to move.

The matrix crosses every property the exemption is gated on:
  * answer carries a [Document N] marker / does not
  * answer names a cited source / names none
  * answer is substantial / is short
  * answer is a refusal / is not
  * judge clears it / judge flags a claim / judge calls it off-topic
  * window carries the composed data-half chunk / does not
  * a deterministic pre-check is outstanding / is not

A cell is ALLOWED to differ only when the new code sets
`citation_format_degraded` — i.e. exactly the case the module exists for.
Any other difference is a regression and the script says so and exits 1.

No LLM, no retrieval, no backend: `call_llm` is stubbed in both modules.

Usage:
    PYTHONPATH=. python scripts/module101_equality_control.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import io
import itertools
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

BASELINE_REF = os.environ.get("M101_BASELINE_REF", "origin/main")


def _load_baseline_verifier():
    """`origin/main`'s verifier.py, imported as a second live module."""
    src = subprocess.run(
        ["git", "show", f"{BASELINE_REF}:src/pipeline/verifier.py"],
        cwd=HERE, capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout
    # `verifier.py` resolves `prompts/verifier.txt` relative to its own file
    # (parent.parent.parent), so the baseline copy has to sit at the same
    # depth with a prompts/ beside it. `prompts/verifier.txt` is untouched by
    # this module, so the current one IS the baseline's prompt.
    root = tempfile.mkdtemp()
    os.makedirs(os.path.join(root, "src", "pipeline"))
    os.makedirs(os.path.join(root, "prompts"))
    io.open(os.path.join(root, "prompts", "verifier.txt"), "w", encoding="utf-8").write(
        io.open(os.path.join(HERE, "prompts", "verifier.txt"), encoding="utf-8").read()
    )
    tmp = os.path.join(root, "src", "pipeline", "verifier_baseline.py")
    io.open(tmp, "w", encoding="utf-8").write(src)
    spec = importlib.util.spec_from_file_location("verifier_baseline", tmp)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["verifier_baseline"] = mod
    spec.loader.exec_module(mod)
    return mod


_CRPC = "1_1898_Code_of_Criminal_Procedure_(Pakistan).pdf"
_PPR = "4_Punjab-Police-Rules-III.pdf"
_AGG = "our own case records (cross-case aggregate)"

_STATUTE_CHUNK = {
    "id": "crpc-35",
    "text": "174. Police to inquire and report on suicide, etc.",
    "metadata": {"source": _CRPC},
}
_RULE_CHUNK = {
    "id": "ppr-1561",
    "text": "25.31. Information of a death in suspicious circumstances.",
    "metadata": {"source": _PPR},
}
_DATA_HALF_CHUNK = {
    "id": "kb-data-half:death_investigation_charging",
    "text": "10 of the 73 FIR(s) that carry a recorded section cite PPC 302.",
    "metadata": {"source": _AGG, "source_tool": "XAGG"},
}
_ANON_CHUNK = {"id": "x", "text": "Some retrieved prose.", "metadata": {"source": "unknown"}}

_LONG = (
    " The officer in charge would open an inquiry, record the circumstances, and "
    "forward the matter onward for further action by the competent authority as "
    "the situation may require, in the manner ordinarily followed."
)

ANSWERS = {
    "cited_named_long": "Section 174 of the Code of Criminal Procedure (Pakistan) applies [Document 1]." + _LONG,
    "cited_unnamed_long": "The provision applies [Document 1]." + _LONG,
    "uncited_named_long": "Section 174 of the Code of Criminal Procedure (Pakistan) applies." + _LONG,
    "uncited_named_agg_long": "Our own case records (cross-case aggregate) show 10 of 73 FIRs cite PPC 302." + _LONG,
    "uncited_unnamed_long": "The provision applies." + _LONG,
    "uncited_named_short": "The Code of Criminal Procedure (Pakistan) applies.",
    "refusal_named_long": (
        "I cannot answer this question. The Code of Criminal Procedure (Pakistan) "
        "material is not publicly available and I do not have access to the "
        "information required to respond." + _LONG
    ),
    "empty": "",
}

WINDOWS = {
    "statute_only": [_STATUTE_CHUNK],
    "statute_plus_data_half": [_STATUTE_CHUNK, _RULE_CHUNK, _DATA_HALF_CHUNK],
    "anonymous_only": [_ANON_CHUNK],
}

JUDGE = {
    "clears": {"grounded": True, "off_topic": False, "leaked_case_id": None,
               "unsupported_claims": [], "reason": "All claims are supported."},
    "flags_a_claim": {"grounded": False, "off_topic": False, "leaked_case_id": None,
                      "unsupported_claims": ["Rule 27.41(3) is in no chunk."],
                      "reason": "One or more claims lack support."},
    "off_topic": {"grounded": False, "off_topic": True, "leaked_case_id": None,
                  "unsupported_claims": [], "reason": "Answers a different question."},
}

PRE_CHECK = {"none": None, "temporal": 2030}


def _stub(verdict):
    async def fake(system_prompt, user_message, **kwargs):
        return json.dumps(verdict)

    return fake


async def main() -> None:
    import src.pipeline.verifier as new_mod

    old_mod = _load_baseline_verifier()

    moved: list[str] = []
    regressions: list[str] = []
    total = 0

    for (a_name, answer), (w_name, window), (j_name, verdict), (p_name, eff) in itertools.product(
        ANSWERS.items(), WINDOWS.items(), JUDGE.items(), PRE_CHECK.items()
    ):
        chunks = [
            {**c, "metadata": {**c["metadata"], **({"effective_from": eff} if eff else {})}}
            for c in window
        ]
        new_mod.call_llm = _stub(verdict)
        old_mod.call_llm = _stub(verdict)
        kw = dict(answer=answer, cited_chunks=chunks, case_id=None,
                  target_date=2026 if eff else None)
        new_r = await new_mod.verify_grounding(**kw)
        old_r = await old_mod.verify_grounding(**kw)
        total += 1

        cell = f"{a_name} | {w_name} | judge={j_name} | pre={p_name}"
        exempted = bool(new_r.get(new_mod.CITATION_FORMAT_DEGRADED_KEY))
        comparable_new = {k: v for k, v in new_r.items()
                          if k not in (new_mod.CITATION_FORMAT_DEGRADED_KEY, "named_source")}

        if comparable_new == old_r:
            if exempted:
                regressions.append(f"{cell}  [flag set but nothing moved]")
            continue
        if exempted and old_r.get("grounded") is False and new_r.get("grounded") is True:
            moved.append(cell)
        else:
            regressions.append(
                f"{cell}\n    old={json.dumps(old_r, ensure_ascii=False)[:200]}"
                f"\n    new={json.dumps(new_r, ensure_ascii=False)[:200]}"
            )

    print(f"cells compared: {total}")
    print(f"cells that MOVED (exemption fired, rejection -> served): {len(moved)}")
    for m in moved:
        print(f"  + {m}")
    print(f"cells that regressed: {len(regressions)}")
    for r in regressions:
        print(f"  ! {r}")
    sys.exit(1 if regressions else 0)


if __name__ == "__main__":
    asyncio.run(main())
