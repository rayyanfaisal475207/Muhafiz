# ============================================================
# Statute Hypothesis — English, statute-vocabulary retrieval queries
#
# PURPOSE (Module 30, GOLD_QA_REMAINING_FIXES_PLAN.md):
# For a legal-knowledge-base question ("does the law require X, and does
# our data reflect it?"), every query string the pipeline generates is a
# paraphrase of the QUESTION — never of the PROVISION. query_expander.py
# is explicitly forbidden from switching language, and
# cross_script_variant.py sends a Latin-script query (English OR
# Roman-Urdu) to Urdu script. The KB legal corpus, meanwhile, is seven
# ENGLISH statute books. So a Roman-Urdu question is embedded and BM25'd
# only as Roman-Urdu and Urdu-script text against an English corpus, and
# the governing provision never enters the candidate pool at all.
#
# Measured on the live corpus before this module existed (Module 30's
# probe, `{"is_global": True}` scope, top-30 per query):
#   - KB3's Police Order 2002 Article 18 chunks: rank 15 / absent
#   - KB8's CrPC s.173 fourteen-day interim-report chunk: absent
#   - KB9's CrPC s.174 inquest chunk: absent
# With one English query naming the statute AND carrying the provision's
# own vocabulary, the same three chunks came back at rank 1, rank 1 and
# rank 6. The chunks were always there; no generated query reached them.
#
# WHY BOTH HALVES OF THE QUERY MATTER:
# A bare statute name is not enough, and this was measured too — "Police
# Order 2002 Article 18" alone returned NONE of the Article 18 chunks in
# the top 30 (the corpus's chunk text mostly does not repeat its own
# section number; the CrPC s.173 interim-report chunk contains the string
# "section 154" and never the string "173"). What retrieves the provision
# is the provision's own wording. The prompt makes that non-optional.
#
# WHY MORE THAN ONE HYPOTHESIS:
# Asked for a single guess, the model live-picked CrPC ss.154/157 for KB3
# ("is the officer who registers a case meant to be the one who
# investigates it?") — a defensible guess about the same subject matter,
# and the wrong book: the answer is Police Order 2002 Article 18. A
# question sitting on the seam between two statute books is common here
# rather than exceptional, so the model returns up to `n` queries and is
# told to spend them on DIFFERENT books rather than on variations of one.
# Retrieval merges them; a wrong guess costs one embedding.
#
# FAILURE MODE:
# Returns [] on LLM error, unparseable output, or the model's explicit
# empty array — the same []-on-failure contract query_expander.py already
# has. The caller folds the variants in only when present, so the pipeline
# degrades to exactly its pre-Module-30 behaviour rather than failing.
#
# RETRIEVAL-ONLY:
# These strings are never shown to a user and never set the answer's
# language. A hallucinated section number costs nothing beyond a slightly
# worse retrieval probe — the answer is still generated from the chunks
# that actually came back, and the evaluator still judges those chunks.
# ============================================================

import logging
import re
from pathlib import Path
from typing import Optional

from src.pipeline.json_extract import call_llm_json

logger = logging.getLogger(__name__)

_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent.parent / "prompts" / "statute_hypothesis.txt"
)
_PROMPT_TEMPLATE = _PROMPT_PATH.read_text(encoding="utf-8")

# Guard rail, same family as query_expander.py's Devanagari filter: the
# whole point of these variants is that they are ENGLISH, so a response
# that came back in Arabic/Urdu script cannot serve that purpose and is
# dropped rather than folded in (cross_script_variant.py already covers
# the Urdu-script direction, and would duplicate it).
_ARABIC_SCRIPT = re.compile("[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]")

# A single retrieval query. Anything longer means the model started
# explaining or answering despite the prompt — truncate rather than
# discard an otherwise-usable query.
_MAX_QUERY_CHARS = 600

# How many candidate statutes to hypothesise. Measured, not guessed — and
# more is NOT better:
#   n=1 — live-picked CrPC ss.154/157 for KB3, whose answer is in the
#         Police Order, 2002. Right book for KB8 and KB9.
#   n=3 — did surface a Police Order "Investigation Wing" hypothesis for
#         KB3, but the third slot forced the model onto a book that does
#         not govern the question at all (the Anti-Rape Act for both KB8
#         and KB9), and those chunks then WON the cross-encoder rerank,
#         pushing the correct CrPC s.173 and s.174 chunks out of the final
#         five.
#   n=2 — one query per book on the seam the question actually sits on,
#         which is what the prompt's book map now asks for directly rather
#         than leaving to a wider guess budget.
#
# [Module 38] The "won the rerank" half of the n=3 rejection above no
# longer describes the code: `cross_rerank_multi()` fuses by reciprocal
# rank now, not by max score across queries, so a wrong hypothesis buys
# one appended slot instead of the whole window. Module 38 re-measured
# that on live pools with --n-hyp 3 and found the third slot no longer
# harmful to KB4 or KB9's retrieval composition, and asked its successor
# to revisit this constant rather than leave it resting on a mechanism
# that had been removed.
#
# [Module 65] Revisited, on the question with the most to gain from a
# third slot — KB2 needs TWO books at once (the Qanun-e-Shahadat's
# confession bar AND the CrPC's own bar on using a police-recorded
# statement at trial), with a third reading of the question (the Punjab
# Police Rules' case diary) that is entirely reasonable and wrong. n=3
# was measured against n=2 on the live store, `{"is_global": True}`,
# top-30 per query, with the fixed prompt:
#   n=2 — hypothesis 1 (Qanun-e-Shahadat) returns Art. 38 at rank 3 and
#         Art. 39 at rank 6; hypothesis 2 (CrPC) returns s.162(1) at rank
#         1 and the s.162 heading at rank 12. All of gold, both books.
#   n=3 — byte-identical first two hypotheses, and the third was a THIRD
#         phrasing of the material already covered, retrieving none of
#         gold's chunks at any rank.
# So the extra slot is affordable now and still buys nothing measured. It
# stays at 2, on evidence rather than on Module 30's obsolete reason.
#
# The cost of a wrong hypothesis is no longer that it takes the whole
# reranked window, but its chunks still occupy a slot in the final five,
# so the guess budget stays tight until some question is measured to need
# a third book.
DEFAULT_HYPOTHESES = 2


async def generate_statute_queries(question: str, n: int = DEFAULT_HYPOTHESES) -> list[str]:
    """
    Generate up to `n` English retrieval queries, each naming a statute
    that could govern `question` together with that provision's own
    vocabulary, and each aimed at a different statute book.

    Retrieval-use only — see this module's docstring. Returns [] when the
    question has no plausible governing provision in this corpus, or on any
    LLM/parse failure, so the caller can fold the queries in only when
    present exactly as it already does for `expand_query()`.
    """
    if not question or not question.strip():
        return []

    system_prompt = _PROMPT_TEMPLATE.replace("{n}", str(n)).replace("{query}", question)

    schema_hint = (
        f"a bare JSON array of at most {n} English query strings, each naming a "
        "statute and continuing with that provision's own wording — or [] if no "
        "listed statute applies"
    )

    try:
        # Same JSON-array contract, and the same conversational-preamble
        # retry, as query_expander.py — this model answers "Sure! Here
        # are..." often enough that a bare parse is not safe.
        queries, raw = await call_llm_json(
            system_prompt=system_prompt,
            user_message=f"Question: {question}",
            temperature=0.0,
            # Same Qwen3-14B thinking-trace headroom every other local call
            # site in this codebase uses (query_expander.py,
            # cross_script_variant.py, query_rewriter.py): the trace eats
            # into max_tokens before the answer appears. cloud_max_tokens
            # stays at the smaller budget — a cloud model has no equivalent
            # hidden trace.
            max_tokens=2000,
            cloud_max_tokens=800,
            validate=lambda r: isinstance(r, list),
            schema_hint=schema_hint,
        )
    except Exception as exc:
        logger.warning("Statute-hypothesis LLM call failed: %s — skipping", exc)
        return []

    if queries is None:
        logger.warning(
            "Statute hypothesis returned no valid JSON after retries: %s",
            (raw or "")[:100],
        )
        return []

    result: list[str] = []
    for candidate in queries:
        if not isinstance(candidate, str) or not candidate.strip():
            continue
        text = candidate.strip()
        if _ARABIC_SCRIPT.search(text):
            logger.warning(
                "Statute hypothesis came back in Arabic/Urdu script, not English — "
                "dropping (%r)", text[:80],
            )
            continue
        text = text[:_MAX_QUERY_CHARS].strip()
        if text:
            result.append(text)

    result = result[:n]
    logger.debug("Statute hypotheses for %r: %s", question[:60], result)
    return result


# ============================================================
# English rendering of the QUESTION (Module 52)
#
# WHY THIS IS SEPARATE FROM generate_statute_queries():
# The statute hypotheses above are paraphrases of the PROVISION, and
# they exist for retrieval. This one is a paraphrase of the QUESTION,
# and it exists for the RELEVANCE GATE — a different consumer with a
# different requirement, which is why nothing above could be reused.
#
# The measured gap (Module 42, artefact
# `evaluation/kb6_evaluator_language_experiment.json`; called directly at
# temperature 0.0 with `prompts/evaluator.txt` unmodified, the chunk set
# held CONSTANT and containing gold's own statutory text in every cell):
#
#                        roman-Urdu   English
#   compound (gold KB6)     1/3         3/3
#   norm-clause only        0/3         3/3
#
# English 6/6, roman-Urdu 1/6 on identical evidence. Module 30 fixed this
# asymmetry for retrieval and for the cross-encoder; the evaluator was the
# one component in the path still reading the raw Roman-Urdu question.
#
# WHY A QUESTION AND NOT A STATUTE PHRASING — all four cheap alternatives
# were measured and all four failed:
#   - statute hypothesis as `rewritten_query`              1/3
#   - statute hypothesis as BOTH evaluator arguments       2/3
#   - the live retry rewrite                               0/3
#   - an English rendering APPENDED to the original        0/3
# Only a genuine English *question* reaches 3/3. The gate is being asked
# "do these documents answer THIS question", so it needs a question, in
# the corpus's language, and nothing else in the field.
#
# FAILURE MODE: returns None on LLM error, unparseable output, an empty
# string, or an Arabic/Urdu-script response (which cannot serve the
# purpose). Every caller falls back to the raw question, so the pipeline
# degrades to exactly its pre-Module-52 behaviour rather than failing.
#
# NOT USER-FACING: like the statute hypotheses, this string is never shown
# to a user and never sets the answer's language. It is read by the
# retriever and by the evaluator only.
# ============================================================

_ENGLISH_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent.parent / "prompts" / "question_english.txt"
)
_ENGLISH_PROMPT_TEMPLATE = _ENGLISH_PROMPT_PATH.read_text(encoding="utf-8")

# A question, not an answer. Anything longer means the model started
# explaining or answering despite the prompt — truncate rather than discard
# an otherwise-usable rendering, matching _MAX_QUERY_CHARS' reasoning.
_MAX_QUESTION_CHARS = 1000


async def render_question_in_english(question: str) -> Optional[str]:
    """
    Return `question` restated as the same question in English, or None.

    Retrieval- and evaluator-use only — see this module's Module 52 block.
    None on any failure, so callers can fall back to the raw question.
    """
    if not question or not question.strip():
        return None

    system_prompt = _ENGLISH_PROMPT_TEMPLATE.replace("{query}", question)

    try:
        rendered, raw = await call_llm_json(
            system_prompt=system_prompt,
            user_message=f"Question: {question}",
            temperature=0.0,
            # Same Qwen3-14B thinking-trace headroom as every other local
            # call site here; the trace eats into max_tokens before the
            # answer appears. A cloud model has no equivalent hidden trace.
            max_tokens=2000,
            cloud_max_tokens=800,
            validate=lambda r: isinstance(r, dict) and isinstance(r.get("question"), str),
            schema_hint='{"question": "the same question, in English"}',
        )
    except Exception as exc:
        logger.warning("English-rendering LLM call failed: %s — skipping", exc)
        return None

    if rendered is None:
        logger.warning(
            "English rendering returned no valid JSON after retries: %s",
            (raw or "")[:100],
        )
        return None

    text = (rendered.get("question") or "").strip()
    if not text:
        return None
    if _ARABIC_SCRIPT.search(text):
        logger.warning(
            "English rendering came back in Arabic/Urdu script — dropping (%r)",
            text[:80],
        )
        return None
    return text[:_MAX_QUESTION_CHARS].strip() or None
