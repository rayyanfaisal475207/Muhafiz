"""
RAG tool — src/pipeline/harness/tools/rag.py (Phase 0, foundation layer).

Thin adapter over the EXISTING retrieval pipeline (AGENT_HARNESS_DESIGN.md
§2.1): `embed_text()` -> `query_similar()` + `fulltext_index.candidate_pool()`
(Milestone A2 — persistent Postgres tsvector/GIN index, replacing the
`get_all_chunks()` full-corpus fetch this used to call) ->
`retrieve_bm25()` -> `rerank_results()` (RRF) -> `cross_rerank()`
(bge-reranker-v2-m3) -> `evaluate_relevance()`, plus the evaluator-feedback
retry loop (`rewrite_for_retry()`) that already surrounds those calls in
`orchestrator.py`'s RAG route today. No new retrieval logic — every call
below is the same function, called the same way, that orchestrator.py
already calls; this module only gives that existing sequence a reusable,
typed entry point instead of it living inline in one giant coroutine.

[PRESERVE — design §2.1] RAG is the FALLBACK TARGET for GRAPH/GRAPH_HYBRID/
SQL/WEB — it has no fallback of its own. Retry is internal
(evaluator-feedback-driven query rewrite, bounded by `config.MAX_RETRIES`);
exhausting it abstains (`status=EMPTY`) rather than reaching for WEB. That
removal was a deliberate scope decision upstream (orchestrator.py's own
`enable_web_search` docstring) — this wrapper preserves it by construction:
it simply never imports or calls anything web-related.

[PRESERVE — design §2.1] Scoping is case-assignment-based, NOT role-based:
no role gate here, matching `RagToolInput`'s docstring ("Any caller who can
see the case can search it").

SCOPE NOTE — case-record synthetic chunk. `orchestrator.py`'s RAG/GRAPH/
GRAPH_HYBRID routes each inject `_case_record_chunk(gateway, case_id)` (the
case's structured Postgres fields, e.g. status/IO/station, as one synthetic
citable chunk) before the evaluator, so a case whose ingested documents
don't restate that data isn't wrongly judged "not relevant." Design §2.1's
own function list for what this tool wraps stops at `evaluate_relevance()`
and does not mention it, so it is deliberately NOT reproduced here — adding
a Postgres case-record dependency the interfaces doc's `RagToolInput` never
names would be scope creep past the documented wrap boundary. Flagged here,
not silently dropped, in case a later phase decides a sub-agent (Case
Summarization is the natural owner) should compose it back in.

SCOPE NOTE — case vs. project scoping. [UPDATED — contract retrofit,
AGENT_HARNESS_IMPLEMENTATION_PLAN.md §10.3] `CallerContext` carries
`active_case_id` only, not a `project_id`; Phase 0 shipped with this
wrapper reading `active_case_id` + `include_global` alone for that reason.
`ExecutionContext` (types.py, plan §10.1) now carries `project_id`
alongside the nested `CallerContext`, restoring a carrier for the
project/global precedence rule this wrapper previously had to drop. This
wrapper's `where` filter now also folds in `execution.project_id` when
present. It never passes an unscoped (`None`/`{}`) filter to
`query_similar`/`fulltext_index.candidate_pool` — doing so is the exact multi-tenant leak
`orchestrator.py`'s own comments document (Phase 8, Bug 1) — so the one
combination that still can't legitimately scope (no case, no project,
`include_global=False`) returns `EMPTY` without calling retrieval at all,
rather than searching unfiltered.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Literal, Optional

from pydantic import Field

from src import config
from src.pipeline.cross_script_variant import generate_cross_script_variant
from src.pipeline.evaluator import evaluate_relevance
from src.pipeline.harness.types import (
    CROSS_CASE_ROLES,
    CallerContext,
    ChunkMetadata,
    EvidenceChunk,
    OnEventCallback,
    PipelineEvent,
    ToolError,
    ToolInput,
    ToolResult,
    ToolStatus,
)
from src.pipeline.query_expander import expand_query
from src.pipeline.query_rewriter import rewrite_for_retry
from src.pipeline.statute_hypothesis import (
    generate_statute_queries,
    render_question_in_english,
)
from src.retrieval.bm25_retriever import retrieve_bm25
from src.retrieval.cross_reranker import cross_rerank, cross_rerank_multi
from src.retrieval.embedder import embed_text
from src.retrieval.fulltext_index import candidate_pool as bm25_candidate_pool
from src.retrieval.reranker import rerank_results
from src.retrieval.vector_store import (
    cap_case_diversity,
    expand_with_neighbors,
    query_similar,
)

logger = logging.getLogger(__name__)


class RagToolInput(ToolInput):
    """
    Retrieve passages relevant to `query_text` within the caller's scope.

    Deliberately says nothing about HOW. Embedding model, fusion strategy,
    reranking, and candidate counts are all free to change without touching
    this contract.

    [PRESERVE — design §2.1] Scoping is case-assignment-based, NOT
    role-based: RAG carries no role gate. Any caller who can see the case
    can search it.
    """

    top_k: Optional[int] = Field(
        default=None, description="Caller's requested result count. None = tool default."
    )
    include_global: bool = Field(
        default=True,
        description=(
            "Whether shared/global reference material is eligible alongside "
            "case-scoped evidence. Composition with project/global scoping "
            "follows existing precedence rules."
        ),
    )


class RagToolResult(ToolResult):
    """
    [PRESERVE — design §2.1] RAG is the FALLBACK TARGET for GRAPH,
    GRAPH_HYBRID, SQL and WEB — it has no onward fallback of its own, so
    `fallback_to_rag` is pinned False.

    Retry is internal (evaluator-feedback-driven query rewrite, bounded by
    a retry budget). Exhausting it abstains — it does NOT silently reach
    for web search. That removal was a deliberate scope decision, not a
    bug: WEB is reachable only via explicit router classification or an
    explicit caller toggle. Do not "restore" it.
    """

    fallback_to_rag: Literal[False] = False
    retries_used: int = Field(
        default=0, description="Internal retry loop iterations consumed. Observability only."
    )
    evaluator_verdict: Optional[Literal["relevant", "not_relevant", "unavailable"]] = Field(
        default=None,
        description=(
            "Relevance gate outcome on the final attempt. `not_relevant` "
            "after retry exhaustion yields status=EMPTY, not FAILED — "
            "retrieval worked, the evidence just did not answer the "
            "question.\n\n"
            "[Reconciliation fix — harness-reconciliation Unit 3] "
            "`unavailable` — THE GATE COULD NOT RUN (the evaluator raised or "
            "returned malformed output). Chunks are passed through UNVETTED "
            "so a flaky evaluator does not take retrieval down with it, but "
            "the evidence carries no relevance guarantee. Previously this "
            "case was silently reported as `relevant` (fail-open with no "
            "distinguishing signal) — a caller could not tell 'the "
            "evaluator confirmed this' from 'the evaluator never actually "
            "ran'. Always accompanied by a caveat in `degradation_caveats`, "
            "which the composing sub-agent MUST propagate into "
            "`SubAgentResult.caveats`."
        ),
    )
    degradation_caveats: list[str] = Field(
        default_factory=list,
        description=(
            "[Reconciliation fix — Unit 3] User-facing qualifications about "
            "HOW this result was produced — currently, that the relevance "
            "gate could not run. Non-empty iff evaluator_verdict == "
            "'unavailable'."
        ),
    )
    # [Gold-QA fix — ROOT_CAUSE_AND_FIXES.md Module 5] True when this
    # search was scoped to the global/shared reference corpus ONLY (no
    # case_id, no project_id, no all_cases — i.e. exactly the legal
    # knowledge-base scope) AND every retrieval attempt returned zero
    # candidates from BOTH semantic and lexical search — the strongest
    # signal available from inside this one request that the underlying
    # corpus itself has nothing in it, distinct from "this corpus has
    # documents but none matched this specific question." Computed from
    # data already fetched during this call (the candidate pools each
    # retry already builds) — no extra query. Only meaningful when
    # status == EMPTY and evaluator_verdict == "not_relevant"; False
    # otherwise (including on the very first candidate-pool exception,
    # where emptiness can't be distinguished from an infra failure).
    global_corpus_appears_empty: bool = Field(
        default=False,
        description=(
            "True when this was a global-only-scoped search (no case/"
            "project/all_cases — the legal-knowledge-base scope) and every "
            "retry found zero candidates from both semantic and lexical "
            "search, suggesting the underlying corpus is empty rather than "
            "merely irrelevant to this question."
        ),
    )


# [Gold-QA fix — Module 8c] Legal / knowledge-base-INTENT detection.
#
# Problem this closes: in "All Cases" mode `_build_where()` returns the
# mixed `{"all_cases": True}` pool, which searches case-narrative chunks AND
# the global/KB legal corpus together in one ranked retrieval. For a pure
# legal-reference question ("which section governs FIR registration?") the
# far more numerous FIR case narratives — which share the question's own
# vocabulary ("FIR", "registration", "154") — out-rank the actual CrPC
# statutory chunks, so the governing-law answer never surfaces (live-
# confirmed on KB1 in Module 8's honest status: "surfaces real FIR case
# documents instead of the legal KB corpus"). Module 8 (chunking) and 8b
# (large-PDF extraction) fixed the corpus; this fixes which corpus a legal
# question searches.
#
# Fix: for a legal-KB-intent query with NO case anchor, narrow the mixed
# `all_cases` pool to the KB-only `{"is_global": True}` scope the tool
# already recognizes (Module 5's `is_global_only_scope`, rag.py:339) — so
# legal chunks are ranked against each other, not drowned by case data. A
# query that also names a specific case/FIR keeps the mixed scope (it
# genuinely needs both), and if KB-only returns nothing the caller falls
# back to the mixed pool (see `rag_tool()`), so this never makes an answer
# strictly worse than today.
#
# Deliberately BROADER than router.py's `_RAG_LEGAL_TEXT_OVERRIDE_PATTERNS`
# (which only catch "what does section N say" — text-OF-a-numbered-section):
# the KB1 shape is "which law/section GOVERNS <practice>", which names no
# section number at all. English / Urdu / Roman-Urdu.
_LEGAL_KB_INTENT_PATTERNS = [
    # "which/what law|section|rule|act|ordinance governs|covers|applies to X"
    re.compile(
        r"\b(which|what)\b.{0,30}\b(law|section|rule|act|ordinance|provision|clause|article)\b"
        r".{0,40}\b(govern|cover|apply|applies|regulat|deal|require|mandate|prescrib)",
        re.IGNORECASE,
    ),
    # "under what/which law|act|section ..." / "under the <Act>"
    re.compile(r"\bunder\s+(what|which)\b.{0,20}\b(law|act|section|ordinance|rule|provision)\b", re.IGNORECASE),
    # "legal requirement|basis|authority for X"
    re.compile(r"\blegal\s+(requirement|basis|authority|provision|ground)s?\b", re.IGNORECASE),
    # Named legal corpora / codes this KB actually holds (CrPC, PPC, PECA,
    # Qanun-e-Shahadat, Police Order/Rules, Anti-Rape Act, PTA Act) —
    # naming one is a strong signal the answer lives in the legal KB, not
    # case files.
    re.compile(
        r"\b(cr\.?p\.?c\.?|code of criminal procedure|p\.?p\.?c\.?|pakistan penal code|"
        r"peca|qanun[- ]e[- ]shahadat|police order|police rules|anti[- ]rape act|pta act)\b",
        re.IGNORECASE,
    ),
    # Urdu: "کون سی دفعہ/قانون ... ہے", "قانونی تقاضا", "کس قانون کے تحت"
    re.compile(r"(کون\s*س[یا]|کس)\s*(دفعہ|قانون|شق|ایکٹ)"),
    re.compile(r"قانونی\s*تقاض"),
    re.compile(r"کس\s*قانون\s*کے\s*تحت"),
    # Roman-Urdu: "kaunsi dafa/qanoon", "kis qanoon ke tehat", "qanooni taqaza"
    re.compile(r"\b(kaun\s*si|kis)\b.{0,15}\b(dafa|qanoon|qanun|shq|act)\b", re.IGNORECASE),
    re.compile(r"\bkis\s+qanoon\s+ke\s+tehat\b", re.IGNORECASE),
    re.compile(r"\bqanoon[iy]\s+taqaz", re.IGNORECASE),
    # ── Module 18 follow-up: patterns mined from the ACTUAL text of the 7
    # KB questions that were still abstaining (KB2/3/4/5/6/8/9) ───────────
    #
    # The patterns above were mined from KB1's shape alone ("which law
    # GOVERNS X"). Checked directly against the other seven KB questions'
    # literal gold text: they matched 0 of 7 — the same "patterns written
    # against hypothetical phrasing" failure Module 11 found for
    # Meta-Analysis's triggers. Live-confirmed consequence: KB2/KB3
    # retrieved from the MIXED case pool, the evaluator correctly rejected
    # the case narratives as irrelevant three times, and the question
    # abstained ("No sufficiently relevant documents were found") even
    # though the governing statute was sitting in the KB corpus.
    #
    # Widening is low-risk BY CONSTRUCTION here (unlike a router override):
    # KB-only is only ever tried FIRST, with the original mixed pool kept
    # as an automatic fallback (see `where_scopes` in `rag_tool()`), so a
    # false positive costs one extra retrieval pass, never an answer.
    #
    # (a) "does/do the law require|expect|say|allow ..." — a normative
    #     question about the law with no "which/what" interrogative, so
    #     the first pattern above can't see it (KB3).
    re.compile(
        r"\b(does|do|is|are|must|should)\b.{0,25}\blaws?\b"
        r".{0,40}\b(require|expect|mandate|say|allow|permit|oblige|treat|define|distinguish)",
        re.IGNORECASE,
    ),
    # (b) "statutory/legal standard|obligation|duty|procedure|framework"
    re.compile(
        r"\b(statutory|legal)\s+(standard|obligation|duty|procedure|framework|threshold)s?\b",
        re.IGNORECASE,
    ),
    # (c) Witness/accused STATEMENT-recording questions (KB2) — squarely
    #     evidence-law territory (CrPC 161/162 statements, Qanun-e-Shahadat),
    #     even when the question never says the word "law" at all.
    re.compile(
        r"\b(witness|accused|suspect|complainant)\b.{0,50}"
        r"\b(statement|said|say|interview|interrogat|deposition|testimony)",
        re.IGNORECASE,
    ),
    #     [Module 63] The same question in Roman-Urdu and Urdu script. The
    #     English form above is why KB2's GOLD text passes this gate while an
    #     ordinary Roman-Urdu rewording of it does not — "gawah ya mulzim ne
    #     jo kaha" names the same two parties and the same act of saying, in
    #     the vocabulary a Punjab officer would actually use. Both halves are
    #     still required, so a case narrative naming an accused cannot fire it.
    re.compile(
        r"\b(gawah\w*|mulzim\w*|mushtaba\w*|mudai\w*)\b.{0,50}"
        r"\b(bayan\w*|kaha|kehta|interview|tafteesh|puchh?[- ]?gachh)",
        re.IGNORECASE,
    ),
    re.compile(r"(گواہ|ملزم|مدعی|مشتبہ).{0,50}(بیان|کہا|انٹرویو|تفتیش)"),
    # (d) Named KB corpora the original list missed — the Forensics
    #     guidelines and Punjab Police Rules are two of the seven PDFs
    #     actually in this corpus (KB6, KB4).
    re.compile(
        r"\b(forensics?\s+guidelines?|punjab\s+police\s+rules|police\s+rules)\b",
        re.IGNORECASE,
    ),
    #     [Module 63] The same two corpora named the Roman-Urdu way. Module
    #     52 measured the consequence directly: "forensics ke usoolon" — an
    #     ordinary rewording of KB6 — returned False from this gate, so the
    #     KB-only scope, the statute hypotheses, the RRF fusion and the
    #     English rendering were all skipped and every chunk the evaluator
    #     judged was an FIR narrative. Requires the corpus name AND its
    #     principles/rules word, so a bare "forensics" mention cannot fire it.
    re.compile(
        r"\bforensics?\b.{0,20}\b(usool\w*|asool\w*|zaabt\w*|zabt\w*|"
        r"hidaya?t\w*|qawaid|qawaed|guidelines?|rules?)\b",
        re.IGNORECASE,
    ),
    re.compile(r"فارنزک.{0,20}(اصول|ضابط|ہدایات|قواعد)"),
    # (e) Urdu: "قانون ... تقاضا/ضروری/لازم" (the law requires ...) — the
    #     noun form, where the existing pattern only caught the adjective
    #     "قانونی تقاضا" (KB5).
    re.compile(r"قانون.{0,40}(تقاض|ضروری|لازم|پابند)"),
    # (f) Urdu: "کوئی باقاعدہ معیار/ضابطہ/طریقۂ کار موجود ہے" (is there a
    #     formal standard/procedure for this) — a norm question that never
    #     uses the word "قانون" (KB4).
    re.compile(r"(باقاعدہ|مقرر[ہہ]?)\s*(معیار|ضابط|اصول|طریق)"),
    re.compile(r"کوئی\s*(باقاعدہ|قانونی|مقررہ)\s*(معیار|ضابط|اصول|طریق)"),
    # (g) Roman-Urdu: "qanoon ... zaroori/lazmi karta hai" (KB8), and
    #     "police ko ... karni hoti hai" (police are required to ..., KB9)
    #     — normative duty phrasing with no interrogative law word.
    re.compile(r"\bqanoo?n\b.{0,50}\b(zaroori|zaruri|lazmi|laazmi|paband|taqaza)", re.IGNORECASE),
    re.compile(
        r"\b(police|tafteesh|tehqeeqat|tehqiqat)\b.{0,50}"
        r"\b(karni\s+hoti\s+hai|karna\s+hota\s+hai|zaroori|lazmi|laazmi)\b",
        re.IGNORECASE,
    ),
]

# Module 18 follow-up, second half: the compound "[legal/procedural norm]
# — and does OUR data actually reflect it?" shape that every one of these
# KB questions takes (KB1 included). Neither half alone is a reliable KB
# signal — "our weapon register" alone is a plain data question, and a
# norm word alone can appear in any narrative — but their CO-OCCURRENCE is
# distinctive to exactly this compound legal-vs-practice question type.
# Kept as a two-signal AND rather than one long regex so each side stays
# readable and independently testable.
_NORM_SIGNAL_RE = re.compile(
    r"\b(law|legal|statut\w+|rule|regulation|guideline|standard|procedure|"
    r"required?|requirement|mandat\w+|oblig\w+|must|supposed to|meant to|expects?)\b"
    r"|قانون|ضابط|معیار|لازم|ضروری|تقاض"
    # [Module 63] Roman-Urdu norm vocabulary, widened from the seven fixed
    # word-forms this line originally carried to their inflected forms. The
    # measured miss is exactly a morphology one: "usool" was here and
    # "usoolon" — the ordinary oblique plural, and the actual word in Module
    # 52's KB6 paraphrase — was not, so \busool\b could not see it. Added
    # alongside: "tareeqa"/"tariqa" (procedure — the English "procedure" is
    # already on the line above), "muqarrara"/"baqaida" (prescribed/formal,
    # the Roman-Urdu twin of the Urdu-script (باقاعدہ|مقررہ) pattern already
    # in the list above) and "miyaar" (standard). Each of these is a norm
    # word only; this half of the AND is never sufficient on its own — the
    # co-occurring our-data signal below is still required.
    r"|\b(qanoo?n\w*|zaroor\w*|zaruri\w*|la+zm\w*|usool\w*|asool\w*|zabt[ae]\w*|"
    r"muqarrar\w*|ba+qaid\w*|ba+qaed\w*|m[ei]yaar\w*|paband\w*|taqaz\w*)\b"
    # "tareeqa"/"tariqa" is the Roman-Urdu for "procedure", which the
    # English half of this pattern already treats as a norm word — but it
    # is also the ordinary word for "manner", and this module's own
    # negative control caught the difference: "cases kis TAREEQE SE
    # station ke hisaab se bante hain" is a plain data question, and with
    # "hamare record" in the same sentence it passed the AND below. The
    # interrogative "kis/kaun se tareeqe" is excluded by lookbehind; the
    # declarative "tafteesh ka tareeqa alag hota hai" (is the PROCEDURE
    # different) is kept, which is the sense that makes it a norm word.
    r"|(?<!kis )(?<!kaun se )(?<!kis se )\btareeq\w*\b"
    r"|(?<!kis )(?<!kaun se )(?<!kis se )\btariq\w*\b",
    re.IGNORECASE,
)
_OUR_DATA_SIGNAL_RE = re.compile(
    r"\b(our|we|us)\b.{0,30}\b(system|data|record|records|recordkeeping|register|"
    r"database|file|files|tracking)\b"
    r"|\bhamara|hamari|hamare\b"
    r"|ہمار[اےی]",
    re.IGNORECASE,
)

# A case/FIR anchor in the query text means the question genuinely needs
# case data too — keep the mixed scope rather than narrowing to KB-only.
# Reuses router.py's own active-case shape (CASE-xxx / FIR-xxx / a bare FIR
# number like "891/24"), plus "this case"/"is case".
_CASE_ANCHOR_RE = re.compile(
    r"\b(CASE|FIR)[-\s]?\d|\b\d{1,4}\s*/\s*\d{2}\b|\bthis case\b|\bin case\b",
    re.IGNORECASE,
)


def _is_legal_kb_intent(query_text: str) -> bool:
    """
    True when the query is a pure legal/knowledge-base-reference question
    with no case anchor — the shape that should search the legal KB corpus
    alone rather than the mixed all-cases pool. See
    `_LEGAL_KB_INTENT_PATTERNS`' own comment for the full rationale.
    """
    if not query_text:
        return False
    if _CASE_ANCHOR_RE.search(query_text):
        return False  # names a specific case — needs the mixed pool
    if any(pat.search(query_text) for pat in _LEGAL_KB_INTENT_PATTERNS):
        return True
    # Module 18 follow-up: the compound "does the law require X — and does
    # OUR data reflect it?" shape, caught by the co-occurrence of a
    # norm signal and an our-data signal (see those patterns' own comment).
    return bool(
        _NORM_SIGNAL_RE.search(query_text) and _OUR_DATA_SIGNAL_RE.search(query_text)
    )


# ═══════════════════════════════════════════════════════════════════════
# [Gold-QA fix — Module 39] THE "AND DOES OUR DATA SHOW IT?" HALF
#
# WHAT THIS CLOSES. Every gold KB answer is COMPOUND: a statutory norm plus
# a figure read off our own case database (KB4 "45 property entries", KB5
# "8 violence-against-women reports", KB6 "32 weapons", KB9 "8 FIRs citing
# PPC 302", and — not closed here, see below — KB3's officer pairs and
# KB8's 26 challans). Module 52 re-baselined all eight KB questions across
# 48 live runs and found **0 of 48** producing gold's data half, in either
# arm. That is not a retrieval defect and no amount of retrieval work can
# fix it: this tool searches a corpus of seven English statute PDFs, and
# the figure the question asks for is not in any of them. The RAG sub-agent
# has no database access at all, so it answers the second clause with "the
# documents do not confirm whether…" — which gold explicitly contradicts,
# because gold gives the number.
#
# WHY HERE AND NOT IN THE DECOMPOSER. `meta_analysis.py::_DECOMPOSITION_PLANS`
# is the obvious prior art and the shape below is deliberately copied from
# it (a question SHAPE -> a canned, deterministically-routed sub-query).
# But Meta-Analysis is only ever reached on the XAGG route
# (`supervisor.py`'s route->sub-agent table), and a legal-KB question routes
# to **RAG** -> Semantic Search. Sending KB questions to Meta-Analysis
# instead would mean a router change, and would cost them the entire
# statutory half Modules 30 and 52 just fixed. The composition therefore
# happens where the norm half already lives: this tool runs the aggregate
# ALONGSIDE retrieval and returns its deterministic rendering as one extra
# citable chunk — exactly the shape `harness/tools/xagg.py` already hands
# the Verifier on the XAGG route. Nothing about the norm half changes: same
# scopes, same evaluator, same retry loop, same chunk order in front of it.
#
# THE GATE IS THREE-WAY AND EVERY CLAUSE MATTERS.
#   1. `_is_legal_kb_intent()` — the same predicate Modules 8c/30/52 use, so
#      this cannot fire on a question the KB path itself does not claim.
#      (Module 63 — that predicate misses some paraphrases — is inherited
#      here by construction and is NOT fixed in this module: a paraphrase
#      that never reaches the legal-KB path never reaches this either.)
#   2. `all_cases` scope + `include_global` — the same two conditions that
#      gate the KB-only scope retry below. A case-scoped question, or a
#      caller who excluded global material, is byte-for-byte unaffected.
#   3. A `_KB_DATA_HALF_PLANS` pattern match. These are narrow, subject-
#      specific and mutually exclusive, and the all-32 EQUALITY control in
#      `tests/test_kb_statute_retrieval.py` pins the resolved plan for every
#      one of the 32 gold questions: exactly KB3, KB4, KB5, KB6, KB8 and
#      KB9 match (Module 77 added the first and the fifth) and the other
#      26 — G2, G5, G3, CR7, M2 and CR3/G1/G6 included — resolve to None.
#
# COST. One aggregate, dispatched CONCURRENTLY with retrieval
# (`asyncio.create_task` before the scope loop, awaited after it), so its
# ~1s of Cypher overlaps a ~90-250s retrieve/evaluate/generate cycle and
# adds no measurable wall clock. Module 53 measured the Meta-Analysis
# sub-query deadline as a SHARED wall clock and this deliberately does not
# reproduce that: the aggregate has its own private `_DATA_HALF_TIMEOUT`,
# and a timeout, a permission denial, an exception or an empty result all
# degrade to "no extra chunk" — the byte-for-byte pre-Module-39 answer.
#
# WHAT MODULE 39 LEFT OPEN, AND HOW IT CLOSED (Module 77). Two of the six
# figures had NO aggregate to dispatch to when this block was written, and
# a third dispatched at the wrong GRAIN. All three are now entries below,
# and each landed exactly as this comment predicted — a one-entry addition,
# with no change to the gate, the concurrency model or the failure
# semantics:
#   - KB3's "68 of 74 registering/investigating officer pairs are the same
#     person" used to resolve to `unsupported_officer`, an honest refusal.
#     Module 74 built `officer_role_pair_overlap` (68/74 = 91.9%, gold
#     exactly, at the ASSIGNMENT-edge grain); entry (5) dispatches to it.
#   - KB8's "26 challans sent to court" used to resolve to
#     `criminal_record_court_crosscheck`, which counts the 33 criminal
#     records rather than the 26 `chalaan_dispatch` rows. Module 75 built
#     `chalaan_dispatch_count`; entry (6) dispatches to it.
#   - KB9's per-section FIR count was served by `statute_court_stage_join`,
#     which is at WHOLE-CASELOAD grain and buries `PPC §302: 10 case(s)` as
#     row six of a 15-row table. Module 76 built `fir_section_case_count`;
#     entry (4) was RE-POINTED at it, in place.
#
# ROUTING IS THE REMAINING VARIABLE, AND IT IS NOT FIXED HERE. A plan entry
# is only ever consulted on the **RAG** route. Modules 65 and 74/75/76 both
# measured KB3 routing to XNETWORK and KB9 to XAGG on this machine, where
# Module 39 measured both on RAG — so two of the six entries below may sit
# unconsulted on any given day. That is a `router.py` question (Module 78),
# deliberately not a `rag.py` one: the entries are correct, cost nothing
# when unreached, and start working the moment the route does.
# ═══════════════════════════════════════════════════════════════════════

# Private deadline for the data-half aggregate. Generous relative to the
# ~1s these aggregates actually take live (measured 0.4-1.6s each), and
# deliberately far below the retrieval loop it runs beside — the whole
# point is that this can never become the thing that makes a KB question
# slow. Exceeding it drops the data half and keeps the norm half.
_DATA_HALF_TIMEOUT = 45.0


@dataclass(frozen=True)
class _KbDataHalfPlan:
    """A compound-KB question SHAPE, and the one canned aggregate sub-query
    that answers its "and does our data show it?" clause."""

    name: str
    patterns: tuple[re.Pattern, ...]
    sub_query: str
    expected_kind: str


# Sub-query wordings are INTERNAL dispatch strings, never shown to a user,
# so they are written in English regardless of the question's own language
# — `xagg.py`'s keyword families are most reliable in English, and each
# string below was checked against `resolve_aggregate_kind()` BEFORE being
# written here (a test keeps them honest). They are dispatched straight to
# `xagg_tool()`, never through `router.py`, so unlike Module 29's plan
# strings they cost zero router calls and cannot be stolen by a router
# override.
_KB_DATA_HALF_PLANS: tuple[_KbDataHalfPlan, ...] = (
    # (1) KB5 — "when violence against a woman is involved, does the law
    #     require a different procedure, and does our data show those steps
    #     were taken?" Gold's data half: 8 women-violence reports on record.
    #     Checked FIRST: it is the narrowest subject here, and a
    #     violence-against-women question can easily also carry the generic
    #     "record"/"register" vocabulary the property plan reads.
    _KbDataHalfPlan(
        name="violence_against_women",
        patterns=(
            re.compile(r"\bviolence\s+against\s+(a\s+)?wom[ae]n\b", re.IGNORECASE),
            re.compile(r"\b(domestic|gender[- ]based)\s+violence\b", re.IGNORECASE),
            re.compile(r"عورت.{0,20}تشدد"),
            re.compile(r"خواتین.{0,20}تشدد"),
            re.compile(r"گھریلو\s*تشدد"),
            re.compile(r"\b(aurat|khatoon|khawateen)\b.{0,25}\btashad?dud\b", re.IGNORECASE),
        ),
        sub_query=(
            "How many domestic violence reports against women are recorded, "
            "and how many are confirmed by a matching FIR, across all cases?"
        ),
        expected_kind="dv_report_fir_match",
    ),
    # (2) KB4 — "is there a standard for how seized items are recorded and
    #     disposed of, and does our property record follow it?" Gold's data
    #     half: 45 property-register entries. The sub-query is COPIED
    #     VERBATIM from Module 50's `_SQ_SEIZED_PROPERTY`, which Module 33
    #     pinned in `tests/test_xagg.py` for exactly this kind of re-use;
    #     a test asserts the two copies stay equal.
    _KbDataHalfPlan(
        name="property_register",
        patterns=(
            re.compile(r"\b(case\s+)?propert(y|ies)\b", re.IGNORECASE),
            re.compile(r"\bmal[- ]?khana\b|\bmaal[- ]?khana\b", re.IGNORECASE),
            re.compile(r"\bseized\s+(item|good|propert)", re.IGNORECASE),
            re.compile(r"پراپرٹی"),
            re.compile(r"مال\s*خانہ|مالخانہ"),
            re.compile(r"تحویل\s*میں\s*لیتی"),
        ),
        sub_query=(
            "How many cases record seized property, and what happens to it — how "
            "many items were sent to a forensic laboratory or held for a deceased's "
            "heirs, across all cases?"
        ),
        expected_kind="seized_property_disposition",
    ),
    # (3) KB6 — "do the forensics guidelines say how a recovered weapon must
    #     be handled, and does our weapon register record that it was?"
    #     Gold's data half: 32 weapons on record — and gold's own verdict is
    #     that the register records NONE of the handling steps, which is the
    #     brief's "correctly stating the data lacks something, where gold
    #     agrees" case. The compliance scan supplies both: the denominator
    #     (32) and what the register does and does not hold.
    _KbDataHalfPlan(
        name="weapon_register",
        patterns=(
            re.compile(r"\bweapons?\s+register\b", re.IGNORECASE),
            re.compile(
                r"\b(recovered|seized|confiscated)\s+(weapon|firearm|arm|pistol|gun)",
                re.IGNORECASE,
            ),
            re.compile(r"\bfirearms?\b", re.IGNORECASE),
            re.compile(
                r"\b(baramad|zabt)\s*(shuda)?\s*(aslah?a|hathyar|hathiyar)\b",
                re.IGNORECASE,
            ),
            re.compile(r"\bhathyar\s+wale?\s+register\b", re.IGNORECASE),
            re.compile(r"(برآمد|ضبط)\s*شدہ\s*(اسلحہ|ہتھیار)"),
            re.compile(r"اسلحہ\s*رجسٹر|ہتھیار\s*رجسٹر"),
        ),
        sub_query=(
            "How many recovered weapons are on record, and how many record no "
            "licence status, across all cases?"
        ),
        expected_kind="weapon_compliance_scan",
    ),
    # (4) KB9 — "a suspicious death must be formally investigated; does our
    #     system record that anywhere?" Gold's data half is a CHARGING-SIDE
    #     figure ("10 FIRs cite PPC 302") plus the honest gap that no
    #     inquest/post-mortem record exists.
    #
    #     RE-POINTED BY MODULE 77, AND THE REASON IS GRAIN, NOT DATA.
    #     Module 39 had only `statute_court_stage_join` to reach for: it is
    #     at WHOLE-CASELOAD grain, caps its statute list at 15 rows and
    #     carries `PPC §302: 10 case(s)` as row six of that table. Module 39
    #     measured what that costs — one live run named the section without
    #     its count, another read PAST the row and asserted that no listed
    #     section pertains to death, which is factually wrong from a chunk
    #     that holds the right row. Module 76 built `fir_section_case_count`,
    #     which answers ONE number at ONE grain, and this entry now
    #     dispatches there. The literal Module 39 string stays pinned in
    #     `tests/test_xagg.py` and must keep resolving to
    #     `statute_court_stage_join` — M4 still owns that family.
    #
    #     THE 10 IS NO LONGER A DIVERGENCE. Module 39 recorded a 10-vs-8 gap
    #     against gold and refused to tune it away; Module 76 re-derived the
    #     10 independently, and gold has since been CORRECTED to 10. Four
    #     derivations agree (Modules 39, 76 twice, and a direct AGE query).
    #     No pattern or wording here is chosen to produce any figure.
    _KbDataHalfPlan(
        name="death_investigation_charging",
        patterns=(
            re.compile(
                r"\b(suspicious|unnatural|custodial)\s+(death|circumstance)",
                re.IGNORECASE,
            ),
            re.compile(r"\bcause\s+of\s+death\b", re.IGNORECASE),
            re.compile(r"\b(inquest|post[- ]?mortem)\b", re.IGNORECASE),
            re.compile(r"\bmashko?ok\b.{0,20}\b(halaat|halat)\b", re.IGNORECASE),
            re.compile(r"\bmaut\s+ki\s+(wajah|waja)\b", re.IGNORECASE),
            re.compile(r"مشکوک\s*حالات"),
            re.compile(r"موت\s*کی\s*وجہ"),
        ),
        sub_query=(
            "How many FIRs cite PPC section 302, across all cases?"
        ),
        expected_kind="fir_section_case_count",
    ),
    # (5) KB3 — "does the law expect the officer who registers a case to be
    #     the one who investigates it, and does that match our data?" Gold's
    #     data half: the same person on 68 of 74 pairs (92%), split in only
    #     6 cases. Module 74 built `officer_role_pair_overlap` for exactly
    #     this and PINNED the sub-query below in `tests/test_xagg.py` as
    #     `_KB3_SQ_OFFICER_ROLE_PAIR`, so this entry is a COPY rather than a
    #     re-derivation; a test asserts the two copies stay equal.
    #
    #     The patterns need BOTH roles named, mirroring the three-signal
    #     `xagg.py::_is_officer_role_pair_comparison()` gate they dispatch
    #     into. A general "which officer handled this?" question must keep
    #     reaching `unsupported_officer`, which is the RIGHT answer for it;
    #     CP6 ("bina kisi tafteeshi afsar ke") is the live proof and the
    #     all-32 control pins it at None.
    #
    #     WHETHER THIS EVER FIRES IS A ROUTER QUESTION. Modules 65 and 74
    #     both measured KB3 routing to **XNETWORK**, not RAG, on this
    #     machine (Module 39 measured RAG). Unconsulted, it costs nothing.
    #     VOCABULARY WIDENED AFTER A PARAPHRASE MISSED, NOT BEFORE. The
    #     first pattern below started as `officer who (first) registers`,
    #     which is KB3's own wording and nothing else: an ordinary English
    #     paraphrase — "is the PERSON who records an FIR supposed to be a
    #     different officer from the one who investigates it?" — missed it
    #     outright, in process, against the real graph. That is Module 56's
    #     finding for the third time and Module 74's for the second, so it
    #     is recorded here rather than quietly fixed. The widened form still
    #     demands BOTH roles; the all-32 control is unchanged by it.
    _KbDataHalfPlan(
        name="officer_role_pair",
        patterns=(
            re.compile(
                r"\b(officer|person|policeman|police\s+officer|official)\s+who\s+"
                r"(first\s+)?(registers?|records?|logs?|files?)\b"
                r"[\s\S]{0,160}\binvestigat",
                re.IGNORECASE,
            ),
            re.compile(
                r"\b(registering|recording)\s+officer\b[\s\S]{0,120}"
                r"\binvestigat(ing|es|or)\b",
                re.IGNORECASE,
            ),
            re.compile(
                r"\binvestigating\s+officer\b[\s\S]{0,120}"
                r"\b(registering|recording)\s+officer\b",
                re.IGNORECASE,
            ),
            re.compile(
                r"\b(darj|mudarrij)\s*(karne\s*wale?)?\s*afsar\b"
                r"[\s\S]{0,120}\btafteesh",
                re.IGNORECASE,
            ),
            re.compile(r"(درج\s*کرنے\s*وال[اے]|مدرج).{0,120}تفتیش"),
        ),
        sub_query=(
            "How many cases record the same person as both the recording "
            "officer and the investigating officer, and how many split those "
            "roles, across all cases?"
        ),
        expected_kind="officer_role_pair_overlap",
    ),
    # (6) KB8 — "if an investigation drags on, must the police report to the
    #     court before it finishes, and does our case-tracking data show that
    #     happened?" Gold's data half: 26 challans sent to court, plus the
    #     honest gap that no interim-report record type exists anywhere in
    #     the schema — the brief's "correctly stating the data lacks
    #     something, where gold agrees" case. The aggregate supplies both:
    #     it reports the dispatch count AND `interim-report record type
    #     present=False`, derived from the record types actually ingested.
    #     Module 75 built `chalaan_dispatch_count` and pinned the sub-query
    #     below as `_KB8_SQ_CHALAAN_DISPATCH`; copied verbatim, test-pinned.
    #
    #     KB8's own gold TEXT never says "challan" — the word is in its gold
    #     ANSWER — so this plan reads the question's INVESTIGATION-to-COURT
    #     shape instead, and the canned sub-query is what carries the challan
    #     vocabulary into `xagg.py`. CR7 is untouched: its Urdu
    #     ("کرمنل ریکارڈ", "عدالتی نتیجے") names neither an investigation
    #     nor a report to a court, and the all-32 control pins it at None.
    #     Unlike KB3 and KB9, KB8 DOES reach RAG (Module 75, 3/3).
    _KbDataHalfPlan(
        name="chalaan_dispatch",
        patterns=(
            re.compile(r"\binterim\s+report\b", re.IGNORECASE),
            re.compile(r"\bchall?a?ans?\b", re.IGNORECASE),
            re.compile(r"چالان|چلان"),
            re.compile(
                r"\btafteesh[\s\S]{0,140}\b(adaa?lat|court)\b",
                re.IGNORECASE,
            ),
            # Widened for the same reason and at the same time as the
            # officer plan's first pattern: "does the law make the police
            # REPORT SOMETHING TO the court" missed a form that demanded
            # `report to the court` with nothing in between.
            re.compile(
                r"\b(investigation|inquiry)\b[\s\S]{0,140}\breport"
                r"[\s\S]{0,40}\bto\s+(the\s+)?(court|magistrate|adaa?lat)\b",
                re.IGNORECASE,
            ),
            re.compile(r"تفتیش[\s\S]{0,140}عدالت"),
        ),
        sub_query=(
            "How many challans have been sent to court, and how many cases "
            "do they cover, across all cases?"
        ),
        expected_kind="chalaan_dispatch_count",
    ),
)


def _match_kb_data_half_plan(query_text: str) -> Optional[_KbDataHalfPlan]:
    """First matching plan, in declaration order (narrowest subject first).

    Pure and deterministic — no LLM call, no I/O — so the all-32 equality
    control can assert over exactly the 32 gold questions. Does NOT itself
    check `_is_legal_kb_intent()`; `rag_tool()` composes the two, so each
    half of the gate stays independently testable.
    """
    if not query_text:
        return None
    for plan in _KB_DATA_HALF_PLANS:
        if any(pat.search(query_text) for pat in plan.patterns):
            return plan
    return None


async def _run_kb_data_half(plan: _KbDataHalfPlan, execution) -> Optional[dict]:
    """Run one plan's aggregate and return its deterministic rendering as a
    citable chunk, or None on ANY failure.

    `xagg_tool` is imported lazily so every caller that never takes this
    branch (GRAPH_HYBRID's within-case composition, every case-scoped RAG
    query) keeps this module's existing import surface.

    Never raises. A permission denial (an investigator has no cross-case
    reach), an upstream failure, a timeout, an empty result, or an
    aggregate that resolved to something other than the family this plan
    named all return None — and the caller then behaves exactly as it did
    before Module 39.
    """
    try:
        from src.pipeline.harness.tools.xagg import XAggToolInput, xagg_tool

        result = await asyncio.wait_for(
            xagg_tool(XAggToolInput(query_text=plan.sub_query, execution=execution)),
            timeout=_DATA_HALF_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "RAG tool: KB data-half aggregate %r timed out after %.0fs — "
            "returning the statutory half alone.",
            plan.name, _DATA_HALF_TIMEOUT,
        )
        return None
    except Exception as exc:  # noqa: BLE001 — degradation is the contract
        logger.warning("RAG tool: KB data-half aggregate %r failed: %s", plan.name, exc)
        return None

    if result.status != ToolStatus.OK or not result.raw_summary_text:
        logger.info(
            "RAG tool: KB data-half aggregate %r returned status=%s — "
            "no data half added.",
            plan.name, result.status,
        )
        return None
    # Defence in depth against `xagg.py`'s keyword chain drifting under us:
    # a sub-query that had silently started resolving to a DIFFERENT family
    # would inject a confidently wrong figure into a KB answer, which is
    # strictly worse than the missing half this module exists to add.
    if result.aggregate_kind != plan.expected_kind:
        logger.warning(
            "RAG tool: KB data-half plan %r expected aggregate %r but got %r — "
            "dropping it rather than citing an unrelated figure.",
            plan.name, plan.expected_kind, result.aggregate_kind,
        )
        return None

    logger.info(
        "RAG tool: KB data-half plan %r answered by aggregate %r (%d chars).",
        plan.name, result.aggregate_kind, len(result.raw_summary_text),
    )
    # Returned in the RAW RETRIEVAL CHUNK SHAPE, not as an `EvidenceChunk`,
    # because it is folded into `reranked` — the same list the relevance gate
    # judges and `_to_evidence_chunk()` converts. `source_tool` in the
    # metadata is what carries the "this one is not a statute" marker through
    # that conversion (see `_to_evidence_chunk()`).
    return {
        "id": f"kb-data-half:{plan.name}",
        "text": result.raw_summary_text,
        "metadata": {
            "source": "our own case records (cross-case aggregate)",
            "source_tool": "XAGG",
            "is_global": True,
        },
    }


def _build_where(
    caller: CallerContext, include_global: bool, project_id: Optional[str] = None
) -> dict:
    """
    Mirrors orchestrator.py's RAG-route where-clause precedence (Module
    4.1): a case, when active, always wins; otherwise a project scope
    (when the caller's `ExecutionContext.project_id` is set — see module
    docstring's project-scoping note, plan §10.3) narrows to that project;
    otherwise fall back to global-only. Never returns an empty dict when
    that would mean "unscoped" — see `_chunk_pool_scope`.

    `project_id` defaults to `None` for backward compatibility with
    existing callers (e.g. GRAPH_HYBRID's within-case composition, which
    passes no project scope) — `None` means "no project scoping applied",
    the same behavior this wrapper had before `ExecutionContext` existed.
    """
    where: dict = {}
    if caller.active_case_id:
        where["case_id"] = caller.active_case_id
    elif project_id:
        where["project_id"] = project_id
    elif caller.role in CROSS_CASE_ROLES:
        # [Scenario-test Finding A] This branch was MISSING here, and its
        # absence is why harness cutover had to be reverted: a supervisor+
        # asking a question with no case selected ("All Cases") fell through
        # to the `is_global` branch below and got global-reference-only
        # scoping. The global corpus is empty in this deployment, so those
        # queries silently returned nothing.
        #
        # orchestrator.py::_build_retrieval_where() has always had this
        # role-based "all_cases" fallback; this tool never got it, so the
        # two drifted. Mirrors that function exactly, on the same role floor
        # (graph_retriever.CROSS_CASE_ROLES) every other cross-case
        # capability in this codebase uses — an investigator with no case
        # selected still falls through to global-only below and does NOT
        # silently gain cross-case reach.
        where["all_cases"] = True
    elif include_global:
        where["is_global"] = True
    return where


async def _retrieve_candidates(
    query_text: str,
    where: dict,
    fetch_top_k: int,
    final_top_k: int,
    is_cross_case: bool,
    statute_queries: Optional[list[str]] = None,
    english_query: Optional[str] = None,
) -> tuple[list[dict], list[dict]]:
    """
    The retrieval half of the RAG primitive — query expansion + cross-script
    variant -> embed -> vector search (deduped, diversity-capped) -> full
    scoped candidate pool -> BM25 — stopping short of RRF fusion.

    Factored out so GRAPH_HYBRID (src/pipeline/harness/tools/graph.py) can
    reuse it instead of re-implementing these steps inline, per
    SUBAGENT_INTERFACES.md §1.3 [RESOLVED-1]: "Compose it as 'GRAPH tool +
    RAG tool, RRF-merged' ... to avoid the current duplication
    (GRAPH_HYBRID re-implements query expansion/cross-script-variant/
    BM25-pool-fetch inline rather than sharing RAG's version)." Returns
    (semantic_results, bm25_results) — the caller RRF-fuses them, possibly
    together with graph chunks.

    `statute_queries`, when given (see `generate_statute_queries()` and its
    call site in `rag_tool()`), are folded in as further retrieval queries —
    the only ones that carry a governing statute's own English vocabulary.

    `english_query` (Module 52), when given, is the QUESTION itself restated
    in English. It is folded in as one more retrieval query, and is not the
    same thing as a statute hypothesis: the hypotheses paraphrase the
    PROVISION and can name the wrong book, while this one is the user's own
    question with nothing added, in the corpus's language. Measured on KB6,
    the chunk carrying gold's statutory half
    (`5_Forensics_guidelines_pdf_62ee00b3_c19`) is missed for exactly the
    lexical reason a Roman-Urdu question is missed everywhere else. Omitted
    (None) for every non-legal-KB query, which keeps their variant list
    byte-for-byte what it was.
    """
    expanded_queries = await expand_query(query_text, n=2)
    cross_script_query = await generate_cross_script_variant(query_text)
    all_queries = [query_text] + expanded_queries + (
        [cross_script_query] if cross_script_query else []
    ) + list(statute_queries or [])
    # Deduped by value: for an already-English question the rendering is the
    # question verbatim (the prompt requires it), and embedding the same
    # string twice buys nothing.
    if english_query and english_query not in all_queries:
        all_queries.append(english_query)

    embeddings = [await embed_text(q) for q in all_queries]

    # Dedupe across the query variants keeping each chunk's BEST similarity,
    # not the first one seen.
    #
    # [Module 30] This used to keep whichever score the FIRST query variant
    # to surface a chunk gave it, and that is not a tie-break detail — it is
    # the score `cap_case_diversity()` (and, for a case-less KB corpus, the
    # whole pool's ordering) sorts on next. Measured live on KB3: the
    # Police Order 2002 Article 18 chunk carrying "shall be investigated by
    # the investigation staff" was surfaced weakly by the original question
    # (0.87, rank 15 of that query's own 30) and strongly by the statute
    # variant (0.91, rank 1) — and, locked to 0.87, it sorted 54th in the
    # merged pool and was cut, while its weaker neighbouring chunks
    # survived. A chunk's place in the pool should reflect the best any
    # variant matched it, which is the entire point of running variants.
    semantic_results: list[dict] = []
    by_id: dict[str, dict] = {}
    for q, emb in zip(all_queries, embeddings):
        for chunk in await query_similar(q, emb, top_k=fetch_top_k, where=where):
            chunk_id = chunk.get("id")
            existing = by_id.get(chunk_id)
            if existing is None:
                by_id[chunk_id] = chunk
                semantic_results.append(chunk)
            elif chunk.get("rrf_score", 0.0) > existing.get("rrf_score", 0.0):
                existing["rrf_score"] = chunk["rrf_score"]

    # [Module 30] The diversity cap exists to stop any ONE CASE's chunks
    # filling the candidate window. A KB-only scope (`{"is_global": True}`,
    # which is exactly the scope Module 8c narrows a legal question to)
    # holds no case-linked chunks at all — every one of them buckets under
    # `case_id=None`, so `per_case_cap` applies to the whole corpus at once
    # and the cap diversifies nothing. It just truncates. Measured on the
    # three KB questions this module fixes: a deduped pool of 71–94
    # statutory chunks cut to 5 before RRF ever saw it, which is what left
    # only one of Article 18's five chunks in the final set. Same reasoning
    # `cap_case_diversity()`'s own docstring gives for a case-scoped query:
    # "nothing to diversify across and must not call this function at all."
    # Sort-and-trim instead, so the pool RRF ranks is still `final_top_k`
    # and still ordered by similarity.
    scope_spans_cases = set(where.keys()) != {"is_global"}
    if is_cross_case and scope_spans_cases:
        semantic_results = cap_case_diversity(
            semantic_results,
            per_case_cap=config.CROSS_CASE_PER_CASE_CAP,
            total_cap=final_top_k,
        )
    elif is_cross_case:
        semantic_results = sorted(
            semantic_results, key=lambda c: c.get("rrf_score", 0.0), reverse=True
        )[:final_top_k]

    combined_query = " ".join(all_queries)
    try:
        # Milestone A2: persistent-index-backed candidate pool — see
        # src/retrieval/fulltext_index.py and orchestrator.py's own
        # equivalent call sites.
        full_candidate_pool = await bm25_candidate_pool(combined_query, where=where)
    except Exception as pool_exc:
        logger.error(
            "RAG tool: fetching full BM25 candidate pool failed: %s. "
            "Falling back to semantic_results only.", pool_exc,
        )
        full_candidate_pool = semantic_results
    bm25_results = retrieve_bm25(combined_query, full_candidate_pool, top_k=final_top_k)

    return semantic_results, bm25_results


def _to_evidence_chunk(raw: dict) -> EvidenceChunk:
    meta = dict(raw.get("metadata") or {})
    # [Gold-QA fix — Module 39] Every chunk this tool retrieves is a RAG
    # chunk and says so; the ONE exception is the data-half aggregate folded
    # in below, which carries its own `source_tool` because it was computed,
    # not retrieved. Semantic Search keys its compound-answer instruction off
    # exactly this field, so the marker has to survive the conversion.
    source_tool = meta.pop("source_tool", None) or "RAG"
    return EvidenceChunk(
        id=raw["id"],
        text=raw.get("text", ""),
        score=raw.get("rerank_score", raw.get("rrf_score")),
        metadata=ChunkMetadata(
            source_tool=source_tool,
            case_id=meta.get("case_id"),
            source_file=meta.get("source"),
            **{k: v for k, v in meta.items() if k not in ("case_id", "source")},
        ),
    )


async def rag_tool(
    tool_input: RagToolInput,
    *,
    on_event: Optional[OnEventCallback] = None,
) -> RagToolResult:
    """
    The RAG primitive: embed -> vector search + BM25 -> RRF -> cross-rerank
    -> evaluate, retrying with evaluator feedback up to `config.MAX_RETRIES`
    times before abstaining.

    `on_event`, if given, is called with a `PipelineEvent` at each internal
    phase boundary (retrieval, reranking, evaluation) so the live trace panel
    shows granular progress instead of one silent "sub-agent ran" gap — and,
    just as importantly, so a long run keeps emitting events and the client's
    stall-detector never fires mid-query. Ignored (no-op) when not passed.
    """
    def _emit(step: str, status: str, detail: str) -> None:
        if on_event is not None:
            on_event(PipelineEvent(step=step, status=status, detail=detail))

    caller = tool_input.execution.caller
    base_where = _build_where(caller, tool_input.include_global, tool_input.execution.project_id)
    if not base_where:
        # No case to scope to, and global material explicitly excluded —
        # nothing legitimate to search. Never fall through to an unscoped
        # query_similar/fulltext_index.candidate_pool call (see module docstring).
        return RagToolResult(status=ToolStatus.EMPTY)

    is_cross_case = not caller.active_case_id
    top_k = tool_input.top_k or config.TOP_K_RETRIEVAL
    fetch_top_k = top_k * config.CROSS_CASE_RETRIEVAL_MULTIPLIER if is_cross_case else top_k

    # [Gold-QA fix — Module 8c] For a legal-KB-intent question that landed in
    # the mixed `all_cases` pool, search the legal KB corpus ALONE first
    # (`{"is_global": True}`), so statutory chunks aren't out-ranked by the
    # far more numerous case narratives (see `_is_legal_kb_intent`' comment).
    # `where_scopes` is the ordered list of scopes to try: KB-only first,
    # then the original mixed pool as a fallback if KB-only finds nothing
    # relevant — so this can only ADD an answer where there was none, never
    # remove one. For every other query it's a one-element list (unchanged
    # behavior). include_global==False is respected: never inject a global
    # scope a caller explicitly excluded.
    where_scopes: list[dict] = [base_where]
    if (
        base_where.get("all_cases")
        and tool_input.include_global
        and _is_legal_kb_intent(tool_input.query_text)
    ):
        where_scopes = [{"is_global": True}, base_where]
        logger.info(
            "RAG tool: legal-KB-intent query in all-cases scope — trying "
            "KB-only corpus first, mixed pool as fallback."
        )

    # [Module 30] For a legal-KB-intent question, generate ONE English query
    # that names the likely governing statute and carries that provision's
    # own vocabulary — see src/pipeline/statute_hypothesis.py for the
    # measured gap it closes (every other query variant is a paraphrase of
    # the QUESTION, and for a Roman-Urdu question none of them is even in
    # the corpus's language). Generated from the ORIGINAL user question, not
    # a retry rewrite: the governing statute does not change when the
    # question is rephrased. None when the model finds no plausible
    # provision, or on any failure — retrieval then behaves exactly as it
    # did before this existed.
    # [Module 52] And, on the same legal-KB path only, restate the QUESTION
    # itself in English — see `render_question_in_english()` for the 2x2 that
    # measured this. Module 30 gave retrieval and the cross-encoder an English
    # phrasing; the relevance gate was left reading the raw Roman-Urdu
    # question, and on identical chunks containing gold's own statutory text
    # it judged the English phrasing relevant 6/6 and the Roman-Urdu one 1/6.
    # Generated from the ORIGINAL question, not a retry rewrite, for the same
    # reason the statute hypotheses are: what the user asked does not change
    # when the retrieval query is rephrased. None on any failure — the
    # evaluator then sees exactly what it saw before this existed.
    statute_queries: list[str] = []
    english_query: Optional[str] = None
    if _is_legal_kb_intent(tool_input.query_text):
        statute_queries = await generate_statute_queries(tool_input.query_text)
        for hypothesis in statute_queries:
            logger.info("RAG tool: statute-hypothesis query: %s", hypothesis[:200])
        english_query = await render_question_in_english(tool_input.query_text)
        if english_query:
            logger.info("RAG tool: English rendering of the question: %s", english_query[:300])

    # [Gold-QA fix — Module 39] The "and does our data show it?" half.
    # Dispatched HERE, before the scope loop, so the aggregate's Cypher runs
    # CONCURRENTLY with retrieval instead of after it — see
    # `_KB_DATA_HALF_PLANS`' comment block for the full rationale, the
    # three-way gate, and the two figures (KB3's, KB8's) this deliberately
    # does not close. The two `where_scopes` conditions are repeated rather
    # than reused so the data half can never fire on a scope the KB-only
    # retry itself would not have been offered.
    data_half_task = None
    data_half_plan = None
    if (
        base_where.get("all_cases")
        and tool_input.include_global
        and _is_legal_kb_intent(tool_input.query_text)
    ):
        data_half_plan = _match_kb_data_half_plan(tool_input.query_text)
        if data_half_plan is not None:
            logger.info(
                "RAG tool: compound legal-KB question — dispatching data-half "
                "plan %r (%s) alongside retrieval.",
                data_half_plan.name, data_half_plan.expected_kind,
            )
            _emit("retrieval", "active",
                  "Checking our own case records alongside the legal corpus…")
            data_half_task = asyncio.create_task(
                _run_kb_data_half(data_half_plan, tool_input.execution)
            )

    # Awaited ONCE, lazily, at the point the gate needs it — memoised so the
    # KB-only→mixed scope retry and every evaluator round reuse the one
    # dispatch rather than re-running the aggregate.
    _data_half_cache: list = []

    async def _get_data_half() -> Optional[dict]:
        if data_half_task is None:
            return None
        if not _data_half_cache:
            _data_half_cache.append(await data_half_task)
        return _data_half_cache[0]

    try:
        last_empty_result: Optional[RagToolResult] = None
        for scope_index, where in enumerate(where_scopes):
            if scope_index > 0:
                _emit("retrieval", "active",
                      "No legal-corpus match — widening to all case documents…")
            result = await _run_retrieval_loop(
                tool_input, where, fetch_top_k, top_k, is_cross_case, _emit,
                statute_queries=statute_queries,
                english_query=english_query,
                get_data_half=_get_data_half if data_half_task is not None else None,
            )
            if result.status == ToolStatus.OK:
                return result
            last_empty_result = result
        # Every scope tried; return the last (EMPTY/FAILED) outcome unchanged.
        #
        # DELIBERATELY NOT rescued by the data half. The gate below sees the
        # data half and still said no; a run that abstains has therefore
        # rejected BOTH halves, and answering it with a bare corpus figure
        # and no norm would be a NEW failure mode — a confident half-answer
        # replacing an honest abstention — not the compound answer gold asks
        # for.
        return last_empty_result if last_empty_result is not None else RagToolResult(status=ToolStatus.EMPTY)
    finally:
        if data_half_task is not None and not data_half_task.done():
            data_half_task.cancel()



async def _run_retrieval_loop(
    tool_input: "RagToolInput",
    where: dict,
    fetch_top_k: int,
    top_k: int,
    is_cross_case: bool,
    _emit,
    statute_queries: Optional[list[str]] = None,
    english_query: Optional[str] = None,
    get_data_half=None,
) -> RagToolResult:
    """
    One full retrieve→rerank→evaluate retry loop against a SINGLE `where`
    scope. Extracted from `rag_tool()` so Module 8c can run it once per
    candidate scope (KB-only, then mixed) without duplicating the loop —
    behavior for a single scope is byte-for-byte the original loop.

    `statute_queries` (Module 30) are the caller's English statute-vocabulary
    phrasings of the question, or empty. They are generated once in
    `rag_tool()` rather than here so the KB-only-then-mixed scope retry does
    not pay for a second identical LLM call. `english_query` (Module 52) is
    the question itself restated in English, generated once in the same
    place and for the same reason, or None.
    """
    current_query = tool_input.query_text
    evaluator_feedback: Optional[str] = None
    retry_count = 0
    # [Gold-QA fix — Module 5] Exactly the legal-knowledge-base scope: no
    # case, no project, no all_cases — global reference material only.
    is_global_only_scope = set(where.keys()) == {"is_global"}
    every_attempt_found_nothing = True

    # [Module 30] Widen the candidate pool for a legal question searching the
    # KB corpus alone — and ONLY there, never globally.
    #
    # This route runs the question through five or six query strings (the
    # question, two paraphrases, a cross-script variant, and Module 30's
    # statute hypotheses), then merges them into one pool of
    # `TOP_K_RETRIEVAL` = 10. That is roughly one and a half slots per
    # variant, which is what makes the merge lossy exactly where it matters:
    # measured on KB3, Article 18's provision spans five consecutive chunks
    # and only ONE of them fitted, so the evaluator saw "shall be
    # investigated by the investigation staff" cut off mid-sentence, without
    # the neighbouring chunk barring the District Police Officer from
    # interfering, and correctly judged the question unaddressed.
    #
    # Scoped deliberately: the KB corpus has no case chunks to crowd out and
    # its documents are small statutory fragments, so a wider pool costs one
    # larger reranker payload and nothing else. A case-scoped or mixed
    # all-cases query keeps `TOP_K_RETRIEVAL` exactly as before. The
    # cross-encoder still cuts to `config.TOP_K_RERANK` either way, so the
    # evaluator's and the answer's input size is unchanged.
    effective_top_k = top_k
    if is_global_only_scope and statute_queries:
        effective_top_k = top_k * config.CROSS_CASE_RETRIEVAL_MULTIPLIER

    while retry_count <= config.MAX_RETRIES:
        if retry_count > 0 and evaluator_feedback:
            try:
                current_query = await rewrite_for_retry(
                    original_message=tool_input.query_text,
                    previous_query=current_query,
                    evaluator_feedback=evaluator_feedback,
                )
            except Exception as exc:
                logger.error("RAG tool: retry rewriter failed: %s", exc)
                # Fall through with the unchanged current_query, matching
                # orchestrator.py's own retry-rewriter exception handling.

        _emit("retrieval", "active",
              "Searching documents…" if retry_count == 0
              else f"Re-searching (attempt {retry_count + 1})…")
        try:
            semantic_results, bm25_results = await _retrieve_candidates(
                current_query, where, fetch_top_k, effective_top_k, is_cross_case,
                statute_queries=statute_queries,
                english_query=english_query,
            )
        except Exception as retr_exc:
            logger.error("RAG tool: retrieval infrastructure failed: %s", retr_exc)
            _emit("retrieval", "error", "Retrieval failed")
            return RagToolResult(
                status=ToolStatus.FAILED,
                error=ToolError(kind="upstream_failure", message=str(retr_exc)),
                retries_used=retry_count,
            )
        _emit("retrieval", "done",
              f"{len(semantic_results)} semantic + {len(bm25_results)} lexical candidates")
        if semantic_results or bm25_results:
            every_attempt_found_nothing = False

        fused = rerank_results(semantic_results, bm25_results, top_k=effective_top_k)

        _emit("reranker", "active", "Re-ranking candidates…")
        try:
            if statute_queries:
                # [Module 30] Getting the provision INTO the fused pool is
                # only half the job — the cross-encoder then scores it
                # against `current_query`, and for a Roman-Urdu question
                # about an English statute that score is noise (measured:
                # every candidate inside 0.0007–0.0022, and the correct
                # CrPC s.173 chunk — RRF rank 1 going in — cut). Scoring the
                # same candidates against the statute phrasing as well
                # retains it.
                #
                # [Module 38] The per-query lists are fused by reciprocal
                # rank rather than by best score: cross-encoder scores are not
                # comparable across queries, and taking the max let whichever
                # phrasing produced the largest numbers take the whole window
                # (measured on KB4 — a wrong "Forensics guidelines" hypothesis
                # scored 0.86-0.97 where the question topped out at 0.16, and
                # took all five slots from the register material the question
                # was actually about). No phrasing is privileged, the user's
                # question included; it is passed first only by convention.
                # See `cross_rerank_multi()`.
                reranked = await cross_rerank_multi(
                    [current_query, *statute_queries], fused, top_k=config.TOP_K_RERANK
                )
            else:
                reranked = await cross_rerank(current_query, fused, top_k=config.TOP_K_RERANK)
        except Exception as exc:
            logger.error("RAG tool: cross-encoder rerank failed: %s. Falling back to RRF order.", exc)
            reranked = fused[: config.TOP_K_RERANK]
        _emit("reranker", "done", f"Top {len(reranked)} selected")

        # [Module 30] Widen each surviving chunk with its immediate
        # neighbours from the same document, for the KB corpus only.
        #
        # Retrieval can be right and the answer still absent: measured on
        # KB3 after the fixes above, the Police Order Article 18 chunk
        # carrying "All registered cases shall be investigated by the
        # investigation staff in the district under" came back at rank 1 on
        # every attempt, and the evaluator still returned relevant=False
        # three times in a row — correctly, because that chunk ends
        # mid-sentence and the words that answer the question ("under the
        # supervision of the head of investigation", "(5) The District
        # Police Officer shall not interfere with the process of
        # investigation") are in the NEXT chunk. This corpus is chunked at
        # roughly 350 characters and a provision routinely spans five of
        # them.
        #
        # Retrieve narrow, read wide — ids, scores and metadata are
        # untouched, so citations still name the chunk actually retrieved.
        # Scoped to the KB-only corpus: case narratives are not split
        # mid-clause the way statute text is, and this must not quietly
        # change what every other route reads.
        if is_global_only_scope and reranked:
            reranked = await expand_with_neighbors(reranked, window=1)

        # [Gold-QA fix — Module 39] Fold the data half in HERE — before the
        # relevance gate, not after it — and append it LAST so every
        # statutory chunk keeps the position, and therefore the
        # `[Document N]` number, it had before this module (the citation
        # contract `verify_grounding()` checks is positional).
        #
        # MEASURED, NOT ASSUMED. The first wiring of this module added the
        # chunk only to the RESULT, after the gate. KB4/KB5/KB6 answered with
        # both halves, and KB9 abstained 3 of 3 — and the gate's own reason,
        # verbatim on all 18 of its refusals, was that the retrieved
        # documents "describe procedural requirements for conducting
        # inquests" and do not say whether OUR system records one. That is a
        # correct verdict on a compound question judged against statute-only
        # evidence: half of what was asked genuinely was not in front of it.
        # The chunk that answers that half existed, 0.6 s away, on the other
        # side of the gate. Showing it to the gate is the whole point.
        #
        # This is the same reasoning `orchestrator.py` already applies with
        # its `_case_record_chunk()` injection (see this module's own SCOPE
        # NOTE): a synthetic, machine-computed chunk placed BEFORE the
        # evaluator so a question is not judged unanswerable merely because
        # the ingested documents do not restate structured data. It does not
        # make the gate say yes — an abstention with the data half present is
        # still an abstention, and `_run_kb_data_half()` returning None
        # leaves this line a no-op.
        if get_data_half is not None:
            data_half = await get_data_half()
            if data_half is not None and not any(
                c.get("id") == data_half["id"] for c in reranked
            ):
                reranked = list(reranked) + [data_half]

        # [Module 30] Which chunks actually reached the evaluator, by id and
        # source file. The evaluator's own `relevant=…` reason is the most
        # useful diagnostic for the KB bucket, but on its own it cannot tell
        # "the right provision was retrieved and misjudged" from "the right
        # provision never arrived" — and Module 19b recorded a probe where an
        # answer cited "section 174" that was not in the chunks being judged
        # at all. Ids + sources only, never chunk text: enough to look the
        # exact passage up in the store, without copying document content
        # into the log.
        logger.info(
            "RAG tool: %d chunk(s) to evaluator (attempt %d): %s",
            len(reranked), retry_count + 1,
            ", ".join(
                f"{c.get('id')}[{(c.get('metadata') or {}).get('source', '?')}]"
                for c in reranked
            ) or "none",
        )

        # [Reconciliation fix — harness-reconciliation Unit 3] Track whether
        # the evaluator itself raised, distinct from it returning a genuine
        # "relevant" verdict. Previously both cases returned identically
        # (evaluator_verdict="relevant"), so a caller could not tell "the
        # evaluator confirmed this evidence" from "the evaluator never ran
        # and we proceeded anyway" — the exact fail-open-without-a-signal
        # gap AGENT_HARNESS_DESIGN.md's own hedging/confidence discussion
        # warns against for a different field. Chunks still pass through
        # unvetted either way (a flaky evaluator must not take retrieval
        # down with it) — what changes is that the caller can now see it.
        evaluator_unavailable = False
        _emit("evaluator", "active", "Checking relevance…")
        try:
            # [Module 52] On the legal-KB path, the gate judges the ENGLISH
            # rendering of the question rather than the raw Roman-Urdu or
            # Urdu-script one. Both arguments, deliberately: the 2x2 in
            # `render_question_in_english()` reaches 3/3 only when the field
            # the gate reads holds a genuine English question and nothing
            # else. Mixing languages across the two arguments was measured
            # and does not work — an English statute phrasing in
            # `rewritten_query` alone is 1/3, appending an English rendering
            # to the original is 0/3.
            #
            # On a RETRY the search query has genuinely moved on, so
            # `rewritten_query` carries `current_query` (the retry rewriter's
            # own output, which is already English) and only the question
            # field is substituted. Attempt 1 has no rewrite to report:
            # `current_query` is the raw question there, and passing it would
            # put the Roman-Urdu text straight back into the prompt.
            #
            # `english_query` is None for every non-legal-KB query and on any
            # rendering failure, and this line is then byte-for-byte what it
            # was before Module 52.
            if english_query:
                evaluation = await evaluate_relevance(
                    english_query,
                    current_query if retry_count > 0 else english_query,
                    reranked,
                )
            else:
                evaluation = await evaluate_relevance(
                    tool_input.query_text, current_query, reranked
                )
        except Exception as exc:
            logger.error("RAG tool: evaluator failed: %s", exc)
            evaluation = {"relevant": True, "reason": "Evaluator failed, proceeding"}
            evaluator_unavailable = True
        _emit("evaluator", "done",
              "Relevant" if evaluation.get("relevant", False) else "Not relevant — retrying")

        if evaluation.get("relevant", False):
            return RagToolResult(
                status=ToolStatus.OK,
                chunks=[_to_evidence_chunk(c) for c in reranked],
                retries_used=retry_count,
                evaluator_verdict="unavailable" if evaluator_unavailable else "relevant",
                degradation_caveats=(
                    ["The relevance of these results could not be automatically verified."]
                    if evaluator_unavailable else []
                ),
            )

        evaluator_feedback = evaluation.get("reason")
        retry_count += 1

    # Retry budget exhausted without a "relevant" verdict — abstain, not an
    # error. [PRESERVE — design §2.1] Never reaches for WEB from here.
    return RagToolResult(
        status=ToolStatus.EMPTY,
        retries_used=retry_count,
        evaluator_verdict="not_relevant",
        global_corpus_appears_empty=is_global_only_scope and every_attempt_found_nothing,
    )


rag_tool.name = "RAG"
