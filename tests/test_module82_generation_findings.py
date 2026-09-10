"""
Modules 82 / 85 / 86 — what the measurement established, pinned.

**This module shipped no change to `semantic_search.py`.** Two generation rules
and a denominator clause were written, measured live over 24 runs, found to be a
net regression (KB4 and its paraphrase went 6-of-6 answering to 2-of-6, with
four verifier rejections, and nothing improved), and deleted — the same posture
Module 71 took toward its own figure roster. `MODULE82_RESULT.md` §2 and
`MODULE85_RESULT.md` §2 carry the wording and the numbers so the next module
does not spend the runs rediscovering it.

What is left to pin is therefore not a rule but the **findings**, and the
contract the attempt had to respect and did:

  * the verifier's one deterministic anti-fabrication pre-check still fires
    where it governs, and is provably inert on the legal-KB path — which is why
    the live forced control, not a mock, is this module's proof;
  * a rejected answer is still never served.

`tests/test_harness_agent_semantic_search.py` is untouched and still passes as
written, because the production file it exercises is byte-identical to `main`.
"""

from __future__ import annotations

import pytest

import src.pipeline.harness.agents.semantic_search as semantic_search_mod
import src.pipeline.verifier as verifier_mod
from src.pipeline.harness.agents.semantic_search import semantic_search
from src.pipeline.harness.tools.rag import RagToolResult
from src.pipeline.harness.types import (
    CallerContext,
    ChunkMetadata,
    EvidenceChunk,
    ExecutionContext,
    Role,
    SubAgentInput,
    SubAgentStatus,
    ToolStatus,
)


# ── helpers (same shapes as tests/test_harness_agent_semantic_search.py) ──

def _chunk(id_="c1", text="the suspect fled the scene", case_id="CASE-001", source="doc.pdf"):
    return EvidenceChunk(
        id=id_,
        text=text,
        metadata=ChunkMetadata(source_tool="RAG", case_id=case_id, source_file=source),
    )


def _agent_input(query_text="who was involved in the theft?"):
    caller = CallerContext(user_id="u1", role=Role.INVESTIGATOR, active_case_id="CASE-001")
    return SubAgentInput(query_text=query_text, execution=ExecutionContext(caller=caller))


def _stub_rag_tool(monkeypatch, result: RagToolResult):
    async def _fake(tool_input, **kwargs):
        return result

    monkeypatch.setattr(semantic_search_mod, "rag_tool", _fake)


def _stub_call_llm(monkeypatch, answer: str):
    async def _fake(system_prompt, user_message, **kwargs):
        return answer

    monkeypatch.setattr(semantic_search_mod, "call_llm", _fake)


def _stub_verify_grounding(monkeypatch, grounded: bool, off_topic: bool = False, reason: str = "ok"):
    async def _fake(answer, cited_chunks, case_id, cross_case_ids=None, target_date=None):
        return {
            "grounded": grounded,
            "off_topic": off_topic,
            "leaked_case_id": None,
            "unsupported_claims": [],
            "reason": reason,
        }

    monkeypatch.setattr(semantic_search_mod, "verify_grounding", _fake)


def _capture_system_prompt(monkeypatch, answer="Register No. 1 is a permanent record [Document 1]."):
    seen: dict[str, str] = {}

    async def _fake(system_prompt, user_message, **kwargs):
        seen["system_prompt"] = system_prompt
        seen["user_message"] = user_message
        return answer

    monkeypatch.setattr(semantic_search_mod, "call_llm", _fake)
    return seen


# ── the deleted change stays deleted, and the reason is in reach ──────────

def test_module82_the_measured_generation_rules_are_not_in_the_prompt():
    """
    A wording lock in the negative direction, which is the useful direction
    here. Both rules were shipped to a live arm and measured:

      | live, 3 runs each, `route=RAG` | before | with the rules |
      |---|---|---|
      | KB4 answered                   | 3 of 3 | **1 of 3** |
      | KB4's Roman-Urdu paraphrase    | 3 of 3 | **1 of 3** |
      | KB2 reached gold's conclusion  | 0 of 3 | 0 of 3 |
      | the 45 stated (Module 86)      | 0 of 6 | 0 of 6 |

    All four new rejections were the verifier correctly refusing a
    **compliance** claim no chunk supports — an over-reach the rules' own
    "state your conclusion plainly and stand behind it" invited. The rules are
    not in the prompt, and this test fails if they come back without the
    measurement being redone.
    """
    prompt = semantic_search_mod._SYSTEM_PROMPT_TEMPLATE
    assert "ANSWER FROM THE DOCUMENTS THAT ARE ABOUT THE QUESTION'S OWN SUBJECT" not in prompt
    assert "DO NOT TAKE THE ANSWER BACK" not in prompt
    assert not hasattr(semantic_search_mod, "_SUBJECT_AND_CONCLUSION_RULES")


def test_module86_the_compound_rule_is_module_39s_unchanged():
    """
    The Module 86 clause ("INCLUDING the totals and denominators it states")
    was measured too, in the same arm, and produced the 45 on **0 of 6** runs —
    the same as without it. Module 39's rule is left exactly as Module 39 wrote
    it, and the measurement is in `MODULE85_RESULT.md` §1.3/§5.
    """
    rule = semantic_search_mod._COMPOUND_ANSWER_RULE
    assert "INCLUDING the totals and denominators" not in rule
    # Module 39's own two halves, still intact.
    assert "This question has TWO parts" in rule
    assert "with the figures exactly as that summary gives them" in rule
    assert "that is a finding, not a failure to" in rule


@pytest.mark.asyncio
async def test_module82_the_documents_block_is_last_in_the_prompt(monkeypatch):
    """
    Module 71's measured lesson, pinned here because this module is the second
    to run into it: interleaving five short lines into the sub-answers section
    of Meta-Analysis' synthesis prompt cost G6 7 of 8 verifier rejections,
    because that prompt's citation rule is held only by proximity to the end.

    In THIS prompt the citation rule is near the top and the documents are
    last. Anyone adding a rule here should add it before the documents block —
    and should read §2 of `MODULE82_RESULT.md` first, because doing exactly
    that is what this module measured and reverted.
    """
    chunks = [_chunk("c1", "some evidence text")]
    _stub_rag_tool(monkeypatch, RagToolResult(status=ToolStatus.OK, chunks=chunks, evaluator_verdict="relevant"))
    seen = _capture_system_prompt(monkeypatch)
    _stub_verify_grounding(monkeypatch, grounded=True)

    await semantic_search(_agent_input())

    prompt = seen["system_prompt"]
    assert prompt.index("Every factual claim MUST cite its source") < prompt.index("--- DOCUMENTS ---")
    assert "--- END OF DOCUMENTS ---" in prompt


# ── the hard constraint: a hallucinated answer is still rejected ──────────

def test_module82_the_fabricated_citation_check_still_fires_where_it_governs():
    """
    The verifier's one deterministic anti-fabrication pre-check, unchanged by
    this module (nothing in `verifier.py`, `validation.py` or
    `prompts/verifier.txt` was touched on this branch) and pinned so it stays
    that way. It governs the "[Document N, CASE-ID]" citation shape over
    case-linked chunks, and it still fires on an invented id there.
    """
    case_chunks = [
        {"id": "psrms_fir_fir-64-26#0", "text": "…", "metadata": {"case_id": "fir-64-26"}},
        {"id": "psrms_fir_fir-65-26#0", "text": "…", "metadata": {"case_id": "fir-65-26"}},
    ]
    assert verifier_mod._check_fabricated_case_ids(
        "The audit confirmed no discrepancy [Document 1, fir-512-26].", case_chunks
    ), "an invented case id inside a citation must be caught before the judge runs"
    # Discriminating, not allergic to identifiers: a real id passes.
    assert not verifier_mod._check_fabricated_case_ids(
        "The audit confirmed no discrepancy [Document 1, fir-64-26].", case_chunks
    )


def test_module82_no_deterministic_pre_check_guards_the_legal_kb_path():
    """
    MEASURED, AND RECORDED RATHER THAN ASSUMED — this is why this module's
    proof of the hard constraint is a **live forced control**
    (`scripts/module82_forced_hallucination_control.py`, `MODULE82_RESULT.md`
    §4c) and not this file.

    KB2's and KB4's chunks come from the global legal-KB corpus
    (`is_global=True`), where no chunk carries a `case_id`. Every one of
    `verify_grounding()`'s deterministic pre-checks is inert on that input:
    `_check_fabricated_case_ids()` returns early by design (its own comment
    records the KB1 false positive that put the early return there),
    `_check_hedging()` never fires because RAG computes no per-chunk
    confidence (`semantic_search.py::_chunk_to_verifier_dict`'s own note), and
    `_check_refusal()`/`_check_no_citation()` only catch a refusal or an
    uncited answer, neither of which a fluent fabrication is.

    So on this path the hallucination guard IS the LLM judge. A unit test
    cannot stand in for it. Filed in §8c.
    """
    kb_chunks = [
        {"id": "4_Punjab-Police-Rules-III_pdf_68bb5d0d_c2197",
         "text": "…which has been in the custody of the police for over three years. "
                 "This register is a permanent record.",
         "metadata": {"case_id": None, "source": "4_Punjab-Police-Rules-III.pdf"}},
        {"id": "kb-data-half:property_register",
         "text": "45 register entries across 28 FIRs; forensic-lab dispatch 13 items",
         "metadata": {"case_id": None, "source": "computed"}},
    ]
    fabricated = (
        "Under rule 27.41(3) every article must be destroyed exactly seven years "
        "after the register is closed [Document 1]. The audit of FIR 512/26 "
        "confirmed our records are fully compliant [Document 2]."
    )
    assert verifier_mod._check_fabricated_case_ids(fabricated, kb_chunks) == []
    assert verifier_mod._check_hedging(fabricated, kb_chunks) == []
    assert verifier_mod._check_refusal(fabricated) is None
    assert verifier_mod._check_no_citation(fabricated) is None


@pytest.mark.asyncio
async def test_module82_a_rejected_answer_is_still_never_served(monkeypatch):
    """
    `SubAgentResult`'s [PRESERVE] rule — "an answer that failed verification is
    NEVER served" — asserted marker-by-marker against the exact fabrication
    `scripts/module82_forced_hallucination_control.py` injects live, so the
    unit test and the live control are testing the same string.

    Live, on the shipped (reverted) code, that control ran 3 of 3: the real
    `verify_grounding()` rejected the fabrication every time — *"The claims
    about rule 27.41(3) and the audit of FIR 512/26 are not supported by any of
    the cited chunks"* — and none of the markers reached the user.
    """
    chunks = [_chunk("c2197", "custody of the police for over three years; a permanent record")]
    _stub_rag_tool(monkeypatch, RagToolResult(status=ToolStatus.OK, chunks=chunks, evaluator_verdict="relevant"))
    fabricated = (
        "Under rule 27.41(3) every article must be destroyed exactly seven years "
        "after the register is closed [Document 1]. The audit of FIR 512/26 "
        "confirmed our records are fully compliant [Document 1]."
    )
    _stub_call_llm(monkeypatch, fabricated)
    _stub_verify_grounding(
        monkeypatch, grounded=False,
        reason="rule 27.41(3), the seven-year period and FIR 512/26 appear in no chunk",
    )

    result = await semantic_search(_agent_input(query_text="are case-property records compliant?"))

    assert result.status == SubAgentStatus.ABSTAINED
    assert result.answer_text is None
    for marker in ("27.41", "seven years", "512/26", "fully compliant"):
        assert marker not in (result.answer_text or "")
