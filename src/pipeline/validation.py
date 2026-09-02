"""
Validation — src/pipeline/validation.py
(AGENT_HARNESS_IMPLEMENTATION_PLAN.md §5's trust-layer table, §7.1/§7.2's
resolved mechanics, §8's build checklist's last unbuilt trust-layer item.)

WHAT THIS IS, AND ISN'T. This is a SECOND OPINION check that runs strictly
AFTER `src.pipeline.verifier.verify_grounding()` has already passed, on the
same answer text and the same cited-chunk list. The Verifier asks "is every
claim traceable to a citation, is there no leakage, is low-confidence
evidence hedged." Validation asks a narrower, different question per
(claim, cited-chunk) pair: "does the cited chunk actually say what the claim
says it does" — catching a claim that is technically cited but subtly
overstates, embellishes, or misstates what its own source contains. Do not
confuse this with `citation_consistency.py` (Phase 8) — that is a different,
deterministic, non-LLM check (citation-INDEX arithmetic, Report Drafting
only). Validation is semantic entailment, applicable to every sub-agent that
generates evidentiary text.

UNIT OF CHECKING — reuses the Verifier's own citation parse, not a second
segmentation method (plan §7.1). `_CITATION_PATTERN` below is the identical
pattern `src.pipeline.verifier._check_leakage()`/`_check_hedging()` and
`src.pipeline.citation_consistency.py` already use — kept as an independent
module-level constant (citation_consistency.py's own precedent) rather than
imported, so this file has no runtime dependency on verifier.py's internals,
but the three can never define "what a citation marker looks like"
differently. `answer_text` is split into sentence-shaped claims; each
`[Document N]` marker inside a claim pairs that claim with `cited_chunks[N-1]`.
A claim with two markers produces two independent pairs (one per source it
cites) — the check is per (claim, chunk) pair, per plan §7.1, not per claim.

TWO TIERS (plan §5's table; the FULL/STRUCTURAL split resolved via
AskUserQuestion before this file was written — see `ValidationTier`):
  - FULL semantic tier: one batched local-LLM call (`prompts/validation.txt`),
    three-way SUPPORTED/PARTIALLY_SUPPORTED/NOT_SUPPORTED + a one-line reason
    per pair, same batching pattern as `src.pipeline.json_extract.py`
    (confirmed still the right precedent before writing this — one call per
    answer, all pairs at once, structured JSON out). Mandatory on Cross-Case
    Linkage / Investigative Analysis / Report Drafting (plan §5).
  - STRUCTURAL-ONLY tier: deterministic, NO LLM call at all. Extracts
    numbers/dates/case-and-FIR-identifiers from each claim and checks they
    appear in (are not contradicted by the absence from) the cited chunk's
    own text. This is a narrower, weaker check by design — it can only ever
    return SUPPORTED or NOT_SUPPORTED (a purely lexical check has no
    principled way to detect a "supported but overstated" claim the way the
    LLM tier can), never PARTIALLY_SUPPORTED. Used for Semantic Search /
    Large-Scale Aggregate / Case Summarization (plan §5). This is also the
    concrete shape plan §7.2's own fallback plan ("narrow the check's scope
    to numeric/name/date contradiction only") already describes — not merely
    what every sub-agent would fall back to if the local-model eval failed,
    but what this tier already is by design.

OUTCOME ON A FLAGGED CLAIM — CAVEAT-ONLY, NEVER BLOCKING (resolved via
AskUserQuestion; see the block comment above `ValidationStatus` in
`src/pipeline/harness/types.py` for the full rationale). This module never
raises to abstain an answer and never asks its caller to. A sub-agent that
finds `ISSUES_FOUND` in the returned status adds a caveat naming the flagged
claim(s) to `SubAgentResult.caveats` and carries the per-claim detail through
`SubAgentResult.validation_claims` — `status`/`answer_text` are unaffected.

FAILS OPEN, THE OPPOSITE POSTURE FROM THE VERIFIER (plan §7.1). If the full
semantic tier's LLM call itself errors (parse failure after retries, a
client exception), the answer has ALREADY passed full grounding
verification — `validate_answer()` catches the failure internally and
returns `ValidationStatus.NOT_RUN` with an empty claim list, never raises.
The one documented exception (unaffected by anything in this module) is
Report Drafting: plan §7.1 says a failed Validation run there renders as a
disclosure line in the DOCUMENT BODY via the EXISTING §2.1.1-§2.1.3
disclosure mechanism, since a generated document outlives the session — that
decision belongs to report_drafting.py's own caller-side logic, not to this
module, which only ever returns a status/claims tuple.

LOCAL-ONLY, PERMANENTLY (plan §7.2) — HOW THIS IS ACTUALLY ENFORCED, STATED
EXPLICITLY SO IT ISN'T RE-DERIVED WRONG LATER. The full semantic tier calls
`call_llm_json()` (json_extract.py) with NEITHER `force_cloud=True` NOR
`escalate_to_cloud_on_failure=True` — i.e. it adds NO opt-in cloud-escalation
path of its own, the same posture `verifier.py`'s own LLM judge call already
has (verifier.py passes neither flag either). This is a deliberate mirror of
the Verifier's existing call shape, not an oversight: "no cloud-escalation
path, not even opt-in" is read here as "this feature does not add anything
resembling XNETWORK's `force_cloud=True` one-shot retry (design §2.5) or
json_extract.py's own `escalate_to_cloud_on_failure` opt-in" — it is NOT read
as "this module must re-implement its own local-only HTTP client bypassing
`call_llm()` entirely." The underlying `call_llm()` function's own
connection-level local-to-cloud fallback (a local endpoint that is
genuinely unreachable) is shared, centrally-controlled platform machinery
gated by `config.AIR_GAP_MODE` — every existing LLM call site in this
codebase (router, evaluator, verifier) relies on that same central gate
rather than each re-implementing its own air-gap check, and Validation does
the same. The structural-only tier makes no LLM call at all and so has no
cloud exposure of any kind, by construction.

PRE-WORK COMPLETED BEFORE THIS SHIPPED (plan §7.2): `data/eval/
validation_eval_set.json` — 32 hand-built (claim, cited-chunk) pairs from
real (synthetic, "source": "synthetic" per their own ground-truth records)
case data, including deliberately overstated/contradicted claims, run
against the local model. See AGENT_HARNESS_IMPLEMENTATION_PLAN.md's own
progress-log entry for this branch for the pass/fail result and the
ship-as-designed-vs-narrow-scope decision it produced.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Literal, NamedTuple, Optional

from src.llm.client import call_llm
from src.pipeline.harness.types import ClaimSupport, ValidationClaimResult, ValidationStatus
from src.pipeline.json_extract import call_llm_json

logger = logging.getLogger(__name__)

# Identical pattern to verifier.py's own _check_leakage()/_check_hedging()
# and citation_consistency.py's _CITATION_PATTERN — see this module's own
# docstring ("UNIT OF CHECKING") for why this is kept as an independent
# constant rather than imported.
_CITATION_PATTERN = re.compile(r"\[Document\s+(\d+)\]", re.IGNORECASE)

# Sentence-boundary split covering Latin ('.', '!', '?') and Urdu ('۔')
# terminators, since this platform's generated/cited text is routinely
# either language (see verifier.py's own _HEDGE_PHRASES comment on why an
# English-only list there was a real, not theoretical, gap).
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?۔])\s+")

# Bounded excerpt length for ValidationClaimResult.claim_excerpt — this is a
# §2 bounded-payload element (SUBAGENT_INTERFACES.md's discipline), not a
# place to carry the full answer text back out.
_MAX_CLAIM_EXCERPT_LEN = 200

ValidationTier = Literal["full", "structural"]

# Numbers, ISO/day-month-year dates, and case/FIR-style identifiers — the
# concrete "numeric/name/date contradiction" surface plan §7.2 names for the
# structural-only tier. Deliberately conservative (few false positives) over
# exhaustive, since a structural false-positive would flag a perfectly good
# claim with no LLM judgment available to override it.
# A number must START and END on a digit. The previous pattern
# (`\d[\d,]*\.?\d*`) let a trailing separator into the captured token, so
# "arrested on 2024-09-22, the suspect" yielded the literal token "22," and
# "PECA 2016, PPC" yielded "2016,". Those never matched the source chunk's
# own "22"/"2016", so every date or statute year followed by a comma was
# reported as an unverifiable identifier — a false positive on correct,
# well-grounded answers (scenario-verify Finding S). Internal separators are
# still allowed so "1,200" stays one token.
_NUMBER_RE = re.compile(r"\d(?:[\d,]*\d)?(?:\.\d+)?")
_CASE_ID_RE = re.compile(r"\b(?:CASE|FIR)-[\w-]+\b", re.IGNORECASE)


def _normalize_number(token: str) -> str:
    """
    Canonical form for comparing a claim's figure against its source text.

    Thousands separators and trailing zeros are presentation, not fact: a
    claim saying "1,200" is supported by a chunk saying "1200", and "09" and
    "9" are the same day. Comparing raw surface strings made both look like
    mismatches, so numbers are compared on this normalized form instead.
    """
    cleaned = token.replace(",", "")
    if not cleaned:
        return token
    try:
        value = float(cleaned)
    except ValueError:
        return cleaned
    if value.is_integer():
        return str(int(value))
    return str(value)


def _extract_numbers_and_ids(text: str, *, strip_citation_markers: bool) -> tuple[set[str], set[str]]:
    """
    Pulls out case/FIR-style identifiers first, then numbers from what's
    left over — so a number embedded inside an identifier (the "011" in
    "CASE-011") is never separately flagged as an unmatched bare number, and
    (on the claim side) the `[Document N]` citation marker's own digit is
    never treated as a fact being claimed.
    """
    if strip_citation_markers:
        text = _CITATION_PATTERN.sub(" ", text)
    ids = set(m.upper() for m in _CASE_ID_RE.findall(text))
    remainder = _CASE_ID_RE.sub(" ", text)
    numbers = {_normalize_number(m) for m in _NUMBER_RE.findall(remainder)}
    return numbers, ids


class _ClaimChunkPair(NamedTuple):
    claim_text: str
    document_index: int
    chunk_text: str


def _extract_claim_chunk_pairs(
    answer_text: str, cited_chunks: list[dict]
) -> list[_ClaimChunkPair]:
    """
    Splits `answer_text` into sentence-shaped claims and pairs each
    `[Document N]` marker found in a claim with `cited_chunks[N-1]`'s text.
    A claim citing two documents yields two independent pairs. An
    out-of-range N (already the Verifier's/citation_consistency's problem,
    not this module's) is silently skipped here rather than raising — this
    function's only job is to find pairs, not to police citation validity a
    second time.
    """
    pairs: list[_ClaimChunkPair] = []
    for sentence in _SENTENCE_SPLIT_RE.split(answer_text.strip()):
        sentence = sentence.strip()
        if not sentence:
            continue
        for match in _CITATION_PATTERN.finditer(sentence):
            n = int(match.group(1))
            if 1 <= n <= len(cited_chunks):
                chunk = cited_chunks[n - 1]
                text = chunk.get("chunk_text") or chunk.get("text") or ""
                pairs.append(_ClaimChunkPair(claim_text=sentence, document_index=n, chunk_text=text))
    return pairs


def _excerpt(text: str) -> str:
    text = text.strip()
    if len(text) <= _MAX_CLAIM_EXCERPT_LEN:
        return text
    return text[: _MAX_CLAIM_EXCERPT_LEN - 1].rstrip() + "…"


# ── Structural-only tier (deterministic, no LLM call) ───────────────────


def _validate_structural(
    pairs: list[_ClaimChunkPair],
) -> tuple[ValidationStatus, list[ValidationClaimResult]]:
    """
    For each pair, every number and CASE-/FIR- identifier appearing in the
    claim must also appear (as a literal substring) somewhere in the cited
    chunk's text. A claim introducing a number/identifier the chunk never
    mentions is flagged NOT_SUPPORTED — this tier never returns
    PARTIALLY_SUPPORTED (see this module's own docstring for why: a purely
    lexical check cannot distinguish "overstated" from "unrelated," so it
    only ever asserts a hard mismatch or stays silent).
    """
    results: list[ValidationClaimResult] = []
    for pair in pairs:
        claim_numbers, claim_ids = _extract_numbers_and_ids(pair.claim_text, strip_citation_markers=True)
        chunk_numbers, chunk_ids = _extract_numbers_and_ids(pair.chunk_text, strip_citation_markers=False)

        missing_numbers = claim_numbers - chunk_numbers
        missing_ids = claim_ids - chunk_ids

        if missing_numbers or missing_ids:
            missing_bits = sorted(missing_numbers | missing_ids)
            results.append(
                ValidationClaimResult(
                    document_index=pair.document_index,
                    claim_excerpt=_excerpt(pair.claim_text),
                    support=ClaimSupport.NOT_SUPPORTED,
                    reason=(
                        f"Claim cites figure(s)/identifier(s) not found in its "
                        f"source text: {', '.join(missing_bits)}."
                    ),
                )
            )
        else:
            results.append(
                ValidationClaimResult(
                    document_index=pair.document_index,
                    claim_excerpt=_excerpt(pair.claim_text),
                    support=ClaimSupport.SUPPORTED,
                    reason="No numeric/date/identifier mismatch found against the cited source.",
                )
            )

    status = (
        ValidationStatus.ISSUES_FOUND
        if any(r.support != ClaimSupport.SUPPORTED for r in results)
        else ValidationStatus.PASSED
    )
    return status, results


# ── Full semantic tier (one batched local-LLM call) ──────────────────────


def _format_pairs_for_prompt(pairs: list[_ClaimChunkPair]) -> str:
    lines: list[str] = []
    for i, pair in enumerate(pairs, start=1):
        lines.append(f'[{i}] CLAIM: "{pair.claim_text}"')
        lines.append(f"    SOURCE: {pair.chunk_text}")
        lines.append("")
    return "\n".join(lines)


_VALID_SUPPORT_VALUES = {s.value for s in ClaimSupport}


def _parse_llm_verdicts(
    raw_result: object, pairs: list[_ClaimChunkPair]
) -> Optional[list[ValidationClaimResult]]:
    """
    Maps the LLM's `[{"pair_id": N, "support": ..., "reason": ...}, ...]`
    response onto `pairs` by position (`pair_id` is 1-based, matching the
    prompt's own numbering). Returns None (triggering the fail-open path)
    if the response isn't a well-formed array covering every pair — a
    partial or malformed response is not partially trusted.
    """
    if not isinstance(raw_result, list):
        return None
    by_id: dict[int, dict] = {}
    for item in raw_result:
        if not isinstance(item, dict):
            return None
        pid = item.get("pair_id")
        support = item.get("support")
        if not isinstance(pid, int) or support not in _VALID_SUPPORT_VALUES:
            return None
        by_id[pid] = item

    results: list[ValidationClaimResult] = []
    for i, pair in enumerate(pairs, start=1):
        item = by_id.get(i)
        if item is None:
            return None
        results.append(
            ValidationClaimResult(
                document_index=pair.document_index,
                claim_excerpt=_excerpt(pair.claim_text),
                support=ClaimSupport(item["support"]),
                reason=str(item.get("reason") or "").strip() or "No reason given.",
            )
        )
    return results


_PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "validation.txt"
_SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")


async def _validate_full_semantic(
    pairs: list[_ClaimChunkPair],
) -> tuple[ValidationStatus, list[ValidationClaimResult]]:
    user_message = f"PAIRS:\n{_format_pairs_for_prompt(pairs)}"

    def _validate_shape(result: object) -> bool:
        return _parse_llm_verdicts(result, pairs) is not None

    # Local-only: no force_cloud, no escalate_to_cloud_on_failure — see this
    # module's own docstring ("LOCAL-ONLY, PERMANENTLY") for why this
    # mirrors verifier.py's own call shape rather than inventing a stricter
    # bypass of call_llm()'s shared, centrally-gated fallback.
    raw_result, raw = await call_llm_json(
        system_prompt=_SYSTEM_PROMPT,
        user_message=user_message,
        temperature=0.0,
        max_tokens=2000,
        cloud_max_tokens=800,
        role="reasoning",
        validate=_validate_shape,
        schema_hint='an array of {"pair_id" (int), "support" (one of "supported"/"partially_supported"/"not_supported"), "reason" (string)}',
        _call_llm=call_llm,
    )

    if raw_result is None:
        logger.warning(
            "Validation: full semantic tier failed to return a usable verdict "
            "after retries — failing OPEN (validation_status=not_run). Raw: %s",
            raw[:150],
        )
        return ValidationStatus.NOT_RUN, []

    results = _parse_llm_verdicts(raw_result, pairs)
    if results is None:
        # _validate_shape should have caught this already, but call_llm_json's
        # validate predicate and this parse must not silently disagree.
        logger.warning("Validation: LLM verdict shape passed the validator but failed re-parse.")
        return ValidationStatus.NOT_RUN, []

    status = (
        ValidationStatus.ISSUES_FOUND
        if any(r.support != ClaimSupport.SUPPORTED for r in results)
        else ValidationStatus.PASSED
    )
    return status, results


# ── Public entry point ────────────────────────────────────────────────────


async def validate_answer(
    answer_text: str,
    cited_chunks: list[dict],
    *,
    tier: ValidationTier,
) -> tuple[ValidationStatus, list[ValidationClaimResult]]:
    """
    Run the Validation trust-layer check over an already-Verifier-passed
    answer. Called AFTER `verify_grounding()` returns grounded=True, never
    before — see this module's own docstring.

    Args:
        answer_text:   The Verifier-passed answer text (same text the
                        Verifier itself checked).
        cited_chunks:  The SAME flattened chunk-dict list handed to
                        `verify_grounding()` (design §5's flat, positionally-
                        indexed shape) — this function indexes into it via
                        the answer's own `[Document N]` markers, exactly like
                        the Verifier's deterministic checks do.
        tier:          "full" (one batched local-LLM entailment call,
                        mandatory on Cross-Case Linkage / Investigative
                        Analysis / Report Drafting) or "structural"
                        (deterministic-only, no LLM call, used elsewhere).

    Returns:
        (ValidationStatus, list[ValidationClaimResult]) — SKIPPED with an
        empty list when the answer carries no `[Document N]` citations to
        check (nothing generated to validate). Never raises: a full-tier
        LLM failure returns NOT_RUN, per this module's fail-OPEN posture.
    """
    pairs = _extract_claim_chunk_pairs(answer_text, cited_chunks)
    if not pairs:
        return ValidationStatus.SKIPPED, []

    if tier == "structural":
        return _validate_structural(pairs)

    try:
        return await _validate_full_semantic(pairs)
    except Exception:
        logger.exception(
            "Validation: full semantic tier raised unexpectedly — failing OPEN "
            "(validation_status=not_run)."
        )
        return ValidationStatus.NOT_RUN, []


def caveats_for_validation(
    status: ValidationStatus, claims: list[ValidationClaimResult]
) -> list[str]:
    """
    Human-readable caveat strings for `SubAgentResult.caveats`, given a
    `validate_answer()` result. Every sub-agent that wires the Validation
    gate calls this the same way rather than each re-deriving its own
    wording, so the caveat text a caller sees is uniform across sub-agents.

    [PRESERVE — caveat-only outcome decision, see ValidationStatus] Returns
    one caveat per flagged claim on ISSUES_FOUND, and a single generic
    caveat on NOT_RUN (the check itself failed, not any specific claim) —
    never changes what the caller does with `status`/`answer_text`, only
    what it may want to say about it.
    """
    if status == ValidationStatus.ISSUES_FOUND:
        return [
            f"A cited claim ([Document {c.document_index}]) could only be "
            f"partially confirmed against its source: {c.reason}"
            if c.support == ClaimSupport.PARTIALLY_SUPPORTED
            else (
                f"A cited claim ([Document {c.document_index}]) could not be "
                f"confirmed against its source: {c.reason}"
            )
            for c in claims
            if c.support != ClaimSupport.SUPPORTED
        ]
    if status == ValidationStatus.NOT_RUN:
        return [
            "A secondary claim-verification check could not be completed for "
            "this answer; the answer already passed full grounding "
            "verification."
        ]
    return []
