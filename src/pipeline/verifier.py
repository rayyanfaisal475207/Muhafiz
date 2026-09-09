# ============================================================
# Verifier Agent — Grounding, Hallucination & Off-topic Gate
#
# PURPOSE:
# A hard gate between generation and delivery. Every route that
# produces an answer from retrieved evidence (RAG, GRAPH,
# GRAPH_HYBRID, SQL, WEB, XGRAPH, XAGG) is verified here before
# the answer reaches the investigator.
#
# DESIGN (mirrors evaluator.py):
#   - call_llm() with role="reasoning" (Qwen3-14B) — same model/
#     cost as evaluator, max_tokens=2000 locally (thinking-trace budget),
#     800 on the cloud fallback (no thinking trace to pad for there)
#   - Deterministic pre-checks run in pure Python first (temporal
#     validity, cross-case leakage, confidence hedging) — these are
#     fast and never need an LLM
#   - LLM judge runs after pre-checks, verifying claim-by-claim
#     grounding and off-topic detection
#   - 2-attempt retry on JSON parse failure, then fail-closed:
#     default = not grounded (never best-effort-guess)
#
# CHECKS:
#   1. Claim-to-citation grounding   (LLM judge)
#   2. Off-topic / generic response  (LLM judge)
#   3. Cross-case leakage            (deterministic Python + LLM)
#   4. Confidence-appropriate hedging (deterministic Python + LLM)
#   5. Temporal validity             (deterministic Python)
#
# OUTPUT:
#   {
#     "grounded": bool,
#     "off_topic": bool,
#     "leaked_case_id": str | None,
#     "unsupported_claims": list[str],
#     "reason": str,
#   }
# ============================================================

import logging
import re
from pathlib import Path
from typing import Optional

from src.llm.client import call_llm
from src.pipeline.json_extract import call_llm_json

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "verifier.txt"
_SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")

# Phrases that satisfy the hedging requirement for low-confidence / unconfirmed links.
# Urdu-script equivalents are required, not optional: cross_case_response.txt
# rule 9 forces the model to answer entirely in the user's preferred language,
# so an English-only list here guarantees every correctly-hedged Urdu-language
# answer fails this check — there is no way for it to ever contain one of the
# English words. Urdu is the majority-corpus language for this platform's
# users, so this was a real (not theoretical) rejection path, not just a gap.
_HEDGE_PHRASES = (
    "unconfirmed", "possible", "pending", "not yet verified",
    "under review", "flagged", "uncertain", "may be",
    "غیر تصدیق شدہ",  # unconfirmed
    "ممکنہ", "ممکن ہے",  # possible / it is possible
    "زیر التواء", "زیر التوا",  # pending
    "تصدیق نہیں ہوئی",  # not yet verified
    "زیر جائزہ", "زیر غور",  # under review
    "نشان زد",  # flagged
    "غیر یقینی",  # uncertain
    "ہو سکتا ہے",  # may be
)

# Characters to scan around a [Document N] citation when checking hedging.
_HEDGE_WINDOW = 250


def _effective_confidence(chunk: dict) -> tuple[Optional[float], str]:
    """
    [AMENDMENT — AGENT_HARNESS_DESIGN.md §7 / types.py's ChunkMetadata.
    confidence_status docstring] Resolves a chunk's confidence + status
    from EITHER of the two shapes this function is actually called with:

      1. The LEGACY, pre-harness shape — a top-level `graph_confidence`
         key set directly on the chunk dict by `graph_retriever.py`
         (still the live shape `orchestrator.py`'s own GRAPH/XGRAPH routes
         pass in today; confirmed by reading graph_retriever.py before
         changing this function — `retrieve_graph()` writes
         `"graph_confidence": confidence` directly on each chunk, not
         nested under `metadata`). Checked FIRST and preserved exactly —
         this is still a live call path, not something this fix may break.
      2. The HARNESS shape — an `EvidenceChunk` flattened to a dict per
         SUBAGENT_INTERFACES.md §2's "Verifier boundary" note
         (`{"id", "text", "metadata": chunk.metadata.model_dump()}`,
         confirmed by reading every sub-agent's own `_chunk_to_verifier_dict()`
         before writing this). Confidence lives at
         `metadata["confidence"]`/`metadata["confidence_status"]`, never at
         a top-level `graph_confidence` key — this function is what
         actually reads that field for the first time; every sub-agent
         built through Phase 9 flattens it correctly but nothing before
         this fix ever consumed it. Design §7's own text names this
         exact gap: "affects the Verifier's hedging check... Worth
         deciding before the Verifier's hedging behavior is relied on in
         the harness."

    Returns `(confidence, status)` where `status` is one of
    `"computed"`/`"not_computed"`/`"check_failed"` — legacy chunks that
    set `graph_confidence` are treated as `"computed"` (a real,
    already-scored value), matching their historical behavior exactly.
    """
    gc = chunk.get("graph_confidence")
    if gc is not None:
        return float(gc), "computed"
    meta = chunk.get("metadata") or {}
    return meta.get("confidence"), meta.get("confidence_status", "not_computed")


def _format_chunks_for_verifier(chunks: list[dict]) -> str:
    """
    Format cited chunks for the verifier prompt.
    Includes confidence and case_id from metadata when present, so the
    LLM judge can apply the hedging rule (check 4) itself.
    """
    lines: list[str] = []
    for i, chunk in enumerate(chunks, start=1):
        meta = chunk.get("metadata") or {}
        source = (
            chunk.get("source_file")
            or meta.get("source")
            or "unknown"
        )
        text = chunk.get("chunk_text") or chunk.get("text") or ""
        conf, conf_status = _effective_confidence(chunk)
        case_id_val = meta.get("case_id")

        header_parts = [f"[{i}] Source: {source}"]
        if case_id_val:
            header_parts.append(f"case_id: {case_id_val}")
        # [Module 61] Declared by the caller, never inferred from the text.
        # prompts/verifier.txt rule 7 keys on this exact marker.
        if meta.get(EXHAUSTIVE_SCOPE_META_KEY):
            header_parts.append(
                "COMPLETE LISTING (this document enumerates EVERY record "
                "matching its stated scope; anything not listed here is "
                "absent from that scope)"
            )
        if conf_status == "check_failed":
            # [AMENDMENT] Never displayed as "no confidence signal" —
            # the LLM judge must see this as an unresolved risk, the same
            # posture the deterministic hedging check below takes.
            header_parts.append("confidence: unknown (check failed)")
        elif conf is not None:
            header_parts.append(f"graph_confidence: {conf:.2f}")
        lines.append(" — ".join(header_parts))
        lines.append(f"Text: {text}")
        lines.append("")

    return "\n".join(lines)


# ============================================================
# [Gold-QA fix - Module 61] EXHAUSTIVE LISTINGS AND NEGATIVE INFERENCE
#
# THE DEFECT. CR3's gold answer asserts a negative - "FIR 64/26 has a
# matching walk-in complaint; 65/26 has none". The evidence is a CMS
# linkage listing that contains 64/26 and does not contain 65/26. The LLM
# judge rejected that answer on 7 of 14 live runs (MODULE57_RESULT.md §4)
# with one verbatim reason:
#
#   "The claim about FIR 65/26's absence from the linkage list is inferred
#    but not directly supported by Document 3, which only lists linked
#    cases without..."
#
# The judge was behaving exactly as prompts/verifier.txt tells it to: rule
# 5 says a claim is unsupported unless a chunk states it. Over a NARRATIVE
# chunk that is right - a retrieved excerpt is a fragment, and "X isn't
# mentioned here" says nothing about whether X exists. Over an EXHAUSTIVE
# listing it is wrong: a complete enumeration is a register, and
# non-membership in a register IS what the register asserts. That is the
# entire semantics of the document.
#
# WHY THIS IS NOT A GENERAL RELAXATION OF GROUNDING. Two guards, both
# deterministic, both required before a negative is treated as supported:
#
#   1. THE CHUNK MUST DECLARE ITSELF EXHAUSTIVE. Only a caller that KNOWS
#      its evidence is a complete enumeration sets
#      `metadata["exhaustive_scope"]`. Today exactly one caller does:
#      meta_analysis.py, for a sub-answer whose `tools_used` is exactly
#      ["XAGG"] - a deterministic aggregate computed over the whole corpus
#      by query, not a retrieved sample. A RAG/GRAPH/WEB chunk is NEVER
#      exhaustive and this code has no way to make it so.
#   2. THE SUBJECT OF THE NEGATIVE MUST GENUINELY BE ABSENT. Every
#      identifier the claim names is checked, in Python, against the
#      exhaustive listing's own text. A claim that "64/26 is absent" from
#      a listing that in fact contains 64/26 is a FABRICATED negative and
#      is still rejected - see
#      `test_fabricated_negative_over_exhaustive_listing_is_still_rejected`.
#
# WHY DETERMINISTIC RATHER THAN A PROMPT RULE ALONE. Both are shipped -
# prompts/verifier.txt rule 7 teaches the judge the distinction, and this
# post-pass makes the outcome reproducible. Module 57's whole finding was
# that the SAME question passed or failed on a coin flip; a fix whose only
# mechanism is another sampled LLM verdict would leave that property
# intact. The prompt rule reduces how often the override is needed; the
# override is what makes the result consistent.
#
# SHARED WITH THE VALIDATION GATE. validation.py imports
# `negative_claim_is_supported_by_exhaustive_listing()` from here and
# applies the identical test to its own per-claim verdicts. Before Module
# 61 the two gates disagreed about this exact claim - the verifier refused
# to serve the answer while the validation gate, on the runs where the
# verifier passed, attached "could only be partially confirmed... does not
# mention FIR 65/26 or its absence from the CMS list". One rule, one
# implementation, both gates.
# ============================================================

# Metadata key a caller sets to declare a chunk a COMPLETE enumeration over
# its stated scope. Deliberately a metadata flag rather than something
# inferred from the text: inferring it would let a generation model talk
# the verifier into treating any listing as complete.
EXHAUSTIVE_SCOPE_META_KEY = "exhaustive_scope"

# Wording that marks a judge-reported claim as a NEGATIVE/absence claim,
# split by WHERE the record being called absent sits relative to the phrase.
# The split is not cosmetic: it is what tells "…does not mention FIR 65/26"
# (subject follows) apart from "FIR 64/26 … does not appear" (subject
# precedes), and getting that backwards is the difference between confirming
# a correct negative and confirming a fabricated one.
#
# Both lists are deliberately narrow and absence-specific. "not stated in
# Document 2", "misattributes", "contradicts" and similar hallucination
# reports match NEITHER — those are the verdicts Module 17's live
# hallucination catch is made of, and they must keep rejecting.

# The record is named AFTER the phrase: "does not mention FIR 65/26",
# "absence of a CMS linkage for FIR 65/26".
_ABSENCE_OBJECT_AFTER_RE = re.compile(
    r"\b(?:does|do|did)\s+not\s+(?:mention|contain|include|list|name|reference|show)\b"
    r"|\bno\s+(?:matching|corresponding|linked|associated|entry|record|mention|reference|such)\b"
    r"|\babsence\s+of\b"
    r"|\bwithout\s+(?:any\s+)?(?:mention|reference)\s+of\b",
    re.IGNORECASE,
)

# The record is named BEFORE the phrase: "FIR 65/26's absence from the list",
# "FIR 65/26 does not appear", "65/26 has none".
_ABSENCE_SUBJECT_BEFORE_RE = re.compile(
    r"\babsen(?:ce|t)\b"
    r"|\b(?:does|do|did)\s+not\s+appear\b"
    r"|\b(?:is|are|was|were)\s+not\s+(?:in|on|listed|present|included|among|linked)\b"
    r"|\bha[sve]+\s+none\b"
    r"|\bmissing\s+from\b"
    r"|\bnot\s+found\b"
    r"|\black(?:s|ing|ed)?\b",
    re.IGNORECASE,
)

# Identifier shapes an absence claim can be ABOUT: "65/26", "fir-65-26",
# "FIR 65/26", "CMS-ISB-2026-0341", "CASE-014", or a bare multi-digit run.
# Whatever the claim names must be checked against the listing itself.
_CLAIM_ID_RES = (
    # The lookahead forces the tail to contain a digit, so an ordinary noun
    # phrase ("CMS complaint", "case tag") never becomes a pseudo-identifier
    # that would satisfy the "names at least one identifier" requirement
    # without actually naming anything checkable.
    re.compile(r"\b(?:FIR|CASE|CMS|CNIC)[-\s]?(?=[A-Z0-9/\-]*\d)[A-Z0-9][A-Z0-9/\-]*", re.IGNORECASE),
    re.compile(r"\b\d+\s*[/\-]\s*\d+(?:\s*[/\-]\s*\d+)*\b"),
    re.compile(r"\b\d{4,}\b"),
)

# An identifier is only usable for the absence check if it is SPECIFIC
# enough to be looked up. "65-26" and "cms-isb-2026-0341" are; the bare
# "26" that falls out of splitting "65/26" is not — it occurs inside
# "fir-64-26" and inside every other 2026 FIR number in the listing, so
# treating it as the subject of the claim would make every negative look
# fabricated. Composite (separator-bearing) tokens and long digit runs
# only; bare 2-3 digit fragments are dropped.
_MIN_BARE_ID_DIGITS = 4


def _is_specific_identifier(token: str) -> bool:
    return "-" in token or (token.isdigit() and len(token) >= _MIN_BARE_ID_DIGITS)

# Tokens that are structurally part of a citation or of the judge's own
# prose rather than the identifier being claimed absent.
_CLAIM_ID_STOPWORDS = frozenset({"document", "fir", "case", "cms", "cnic"})


def _normalize_identifier(token: str) -> str:
    """Canonical form for comparing an identifier written one way in a claim
    against the same identifier written another way in a listing: "FIR
    65/26", "65/26" and "fir-65-26" all reduce to a form in which "65-26"
    is a substring. Lowercased; "/", whitespace and repeated separators
    collapsed to a single "-"."""
    token = token.strip().lower()
    token = re.sub(r"[\s/]+", "-", token)
    token = re.sub(r"-{2,}", "-", token)
    return token.strip("-")


def _identifier_tokens(text: str) -> set[str]:
    """Identifier-shaped tokens in `text`, normalized. `[Document N]`
    markers are stripped first - the citation index is provenance
    formatting, never the subject of a claim (the same discipline
    `_numbers_in(strip_citations=True)` already applies)."""
    text = _CITATION_MARKER_RE.sub(" ", text or "")
    tokens: set[str] = set()
    for pattern in _CLAIM_ID_RES:
        for m in pattern.finditer(text):
            norm = _normalize_identifier(m.group(0))
            if not norm or norm in _CLAIM_ID_STOPWORDS:
                continue
            # "fir-65-26" carries the same information as "65-26"; keep the
            # bare tail too so a claim written with the prefix still matches
            # a listing written without it, and vice versa.
            candidates = [norm]
            stripped = re.sub(r"^(?:fir|case|cms|cnic)-", "", norm)
            if stripped and stripped != norm:
                candidates.append(stripped)
            tokens.update(c for c in candidates if _is_specific_identifier(c))
    return tokens


def _chunk_is_exhaustive(chunk: dict) -> bool:
    return bool((chunk.get("metadata") or {}).get(EXHAUSTIVE_SCOPE_META_KEY))


def _chunk_text(chunk: dict) -> str:
    return chunk.get("chunk_text") or chunk.get("text") or ""


def exhaustive_chunk_texts(chunks: list[dict]) -> list[str]:
    """The text of every chunk whose caller declared it a complete
    enumeration over its stated scope. Empty list = no chunk did, and no
    negative inference is licensed anywhere in this answer."""
    return [_chunk_text(c) for c in (chunks or []) if _chunk_is_exhaustive(c)]


# "Document 3" as the judge writes it in its own prose — bare, not the
# answer's bracketed "[Document 3]" marker.
_JUDGE_DOCUMENT_REF_RE = re.compile(r"\bDocument\s+(\d+)\b", re.IGNORECASE)


def listings_a_claim_is_about(claim_text: str, chunks: list[dict]) -> list[str]:
    """
    Which complete listing(s) should a judge-reported absence claim be
    checked against?

    [Module 61, measured live] Pooling every exhaustive chunk was wrong and
    the live runs proved it. CR3 hands the Verifier THREE XAGG sub-answers,
    all of them complete enumerations: a filtered FIR listing that names
    both 64/26 and 65/26, a CMS linkage listing that names only 64/26, and a
    person-recurrence listing. The judge's claim is about membership in the
    CMS LINKAGE listing specifically — and 65/26 does appear in a different
    listing, for a different question, so a pooled check concluded
    "fabricated" and the caveat stayed on every run. Absence is always
    absence FROM A PARTICULAR REGISTER.

    So: when the claim names a document ("…not directly supported by
    Document 3"), that document's listing is the one to check, and only if
    it is exhaustive. When it names none, fall back to requiring absence
    from EVERY exhaustive listing — strictly more conservative than picking
    one, and the right default when there is nothing to disambiguate with.
    """
    if not chunks:
        return []
    referenced = [
        int(m.group(1)) for m in _JUDGE_DOCUMENT_REF_RE.finditer(claim_text or "")
    ]
    in_range = [n for n in referenced if 1 <= n <= len(chunks)]
    if in_range:
        named = [chunks[n - 1] for n in in_range]
        # A claim pinned to a NON-exhaustive document licenses nothing, even
        # if some other chunk in the answer happens to be a listing.
        if not all(_chunk_is_exhaustive(c) for c in named):
            return []
        return [_chunk_text(c) for c in named]
    return exhaustive_chunk_texts(chunks)


def _identifier_spans(text: str) -> list[tuple[int, int, set[str]]]:
    """`(start, end, tokens)` for every identifier-shaped run in `text`,
    ordered by position — the positional counterpart of
    `_identifier_tokens()`. Only COMPOSITE identifiers (a separator after
    normalization, e.g. "65-26", "cms-isb-2026-0341") are kept as possible
    subjects: a bare four-digit run is far more often a statute year ("PECA
    2016") than a record id, and mistaking one for the subject of an absence
    claim would confirm a negative nobody made."""
    text = _CITATION_MARKER_RE.sub(" ", text or "")
    spans: list[tuple[int, int, set[str]]] = []
    for pattern in _CLAIM_ID_RES:
        for m in pattern.finditer(text):
            norm = _normalize_identifier(m.group(0))
            if not norm or norm in _CLAIM_ID_STOPWORDS:
                continue
            candidates = [norm]
            stripped = re.sub(r"^(?:fir|case|cms|cnic)-", "", norm)
            if stripped and stripped != norm:
                candidates.append(stripped)
            tokens = {c for c in candidates if "-" in c}
            if tokens:
                spans.append((m.start(), m.end(), tokens))
    spans.sort()
    return spans


def _absence_subjects(claim_text: str) -> Optional[list[set[str]]]:
    """
    Which record does each absence phrase in `claim_text` say is missing?

    Returns one token-set per absence phrase found, or None when the claim
    contains no absence phrase at all, or when any phrase's subject cannot
    be resolved to an identifier. None means "this function cannot confirm
    anything about this claim" and the judge's own verdict stands — that
    conservative default is what keeps a vague absence claim ("the record is
    incomplete") from being waved through.

    Direction matters and is why the two phrase families exist separately.
    "…does not mention FIR 65/26" names its record AFTER the phrase;
    "FIR 64/26 … does not appear" names it BEFORE. Resolving both the same
    way would, on a sentence naming one present and one absent record,
    silently pick the wrong one — and picking the wrong one is exactly how a
    fabricated negative would get confirmed.
    """
    text = _CITATION_MARKER_RE.sub(" ", claim_text or "")
    spans = _identifier_spans(claim_text)

    object_after = list(_ABSENCE_OBJECT_AFTER_RE.finditer(text))
    after_ranges = [(m.start(), m.end()) for m in object_after]
    subject_before = [
        m for m in _ABSENCE_SUBJECT_BEFORE_RE.finditer(text)
        # "absence of X" is an object-after phrase; the bare "absence"
        # inside it must not be resolved a second time, backwards.
        if not any(a <= m.start() < b for a, b in after_ranges)
    ]
    if not object_after and not subject_before:
        return None
    if not spans:
        return None

    subjects: list[set[str]] = []
    for m in object_after:
        following = next((sp for sp in spans if sp[0] >= m.end()), None)
        if following is None:
            return None
        subjects.append(following[2])
    for m in subject_before:
        preceding = [sp for sp in spans if sp[1] <= m.start()]
        chosen = preceding[-1] if preceding else next(
            (sp for sp in spans if sp[0] >= m.end()), None
        )
        if chosen is None:
            return None
        subjects.append(chosen[2])
    return subjects or None


def negative_claim_is_supported_by_exhaustive_listing(
    claim_text: str, exhaustive_texts: list[str]
) -> bool:
    """
    True when `claim_text` — a JUDGE-AUTHORED statement of what could not be
    confirmed, i.e. an entry in the Verifier's `unsupported_claims` or a
    Validation claim's `reason` — says nothing more than that some record is
    absent from a listing, and every record it names that way is genuinely
    absent from a complete enumeration.

    All of these are required, and each one is what stops this from being a
    general relaxation of grounding:

      * there is at least one chunk the CALLER declared exhaustive;
      * every absence phrase in the claim resolves to a named, composite
        identifier (`_absence_subjects()`), so a misattribution, an invented
        figure or a vague "the record is incomplete" never qualifies;
      * every one of those identifiers is confirmed ABSENT from the
        listing's own text. A claim asserting the absence of something the
        listing actually contains is a fabricated negative and returns
        False.

    The judge's own wording is the unit deliberately, rather than the
    answer's sentence: a sentence routinely mixes a present record and an
    absent one ("64/26 has a linked complaint, while 65/26 does not appear"),
    whereas the judge's reason states precisely the one thing it could not
    confirm. Checking the sentence would make the function's verdict depend
    on which other, unrelated facts happened to share a sentence with the
    negative.
    """
    if not exhaustive_texts:
        return False
    subjects = _absence_subjects(claim_text)
    if not subjects:
        return False

    listing_ids: set[str] = set()
    listing_blob = ""
    for text in exhaustive_texts:
        listing_ids |= _identifier_tokens(text)
        listing_blob += " " + _normalize_identifier(text)

    for tokens in subjects:
        for token in tokens:
            if token in listing_ids:
                return False
            # Substring check as well: a listing rendering "fir-64-26" must
            # count as containing the claim's "64-26".
            if token in listing_blob:
                return False
    return True


def _check_temporal(chunks: list[dict], target_date: Optional[int]) -> list[str]:
    """
    Deterministic pre-check: flag chunks whose effective date range
    excludes the target_date (query year as int, e.g. 2025).
    Returns a list of issue strings (empty = no temporal problems).
    """
    if not target_date or not chunks:
        return []
    issues: list[str] = []
    for chunk in chunks:
        meta = chunk.get("metadata") or {}
        source = meta.get("source", "unknown")
        ef = meta.get("effective_from")
        et = meta.get("effective_to")
        if ef and ef > target_date:
            issues.append(
                f"Document '{source}' (effective_from={ef}) is not yet effective "
                f"for target date {target_date}."
            )
        elif et and et < target_date:
            issues.append(
                f"Document '{source}' (effective_to={et}) has expired "
                f"for target date {target_date}."
            )
    return issues


def _check_leakage(
    answer: str,
    chunks: list[dict],
    active_case_id: Optional[str],
    cross_case_ids: Optional[list[str]],
) -> Optional[str]:
    """
    Deterministic pre-check: scan [Document N] citations in the answer,
    map them back to chunks, and check that every cited chunk's case_id
    belongs to the active case or the explicitly allowed cross-case set.

    Returns the first offending case_id found, or None if clean.
    Logs an ERROR when leakage is found because it indicates a
    retrieval-time filtering bug, not just a generation quality issue.
    """
    if not active_case_id or active_case_id == "cross_case":
        return None

    allowed = set(cross_case_ids or [])
    allowed.add(active_case_id)

    # Match [Document N] tags in the answer (case-insensitive)
    for m in re.finditer(r"\[Document\s+(\d+)\]", answer, re.IGNORECASE):
        n = int(m.group(1))
        if n < 1 or n > len(chunks):
            continue
        chunk = chunks[n - 1]
        meta = chunk.get("metadata") or {}
        chunk_case = meta.get("case_id")
        if chunk_case and chunk_case not in allowed:
            logger.error(
                "SECURITY: Cross-case leakage detected — chunk '%s' (case_id='%s') "
                "cited in answer for case '%s'. This indicates a retrieval-time "
                "filtering bug.",
                chunk.get("id", f"chunk-{n}"),
                chunk_case,
                active_case_id,
            )
            return chunk_case
    return None


def _check_fabricated_case_ids(answer: str, chunks: list[dict]) -> list[str]:
    """
    [Scenario-test Finding J] Deterministic pre-check: cross-case answers cite
    a case id inside a "[Document N, <case-id>]" bracket (see
    prompts/cross_case_response.txt's own rules 2/8/11 and every worked
    example there — e.g. "[Document 3, CASE-005]", with no "CASE-ID:" label
    anywhere in the prompt's instructed format). Verify every case id written
    INSIDE a citation actually exists among the cited chunks' own metadata.

    Found live: a cross-case answer cited "CASE-ID: CR-C101-1", "CR-C102-1"
    and "CR-C105-1" alongside real `fir-NNN-26` ids. None of those `CR-*` ids
    exist anywhere in the corpus — not as a case_id, an external_id, a source,
    or even a substring of any chunk text. The model invented them and
    presented them as provenance, which is worse than an uncited claim: it
    looks like a verifiable reference and doesn't resolve to anything.

    [PRESERVE] The regex below matches the id with or without a "CASE-ID:"
    label. That label-bound match was the ONLY form this check recognized
    for a while — confirmed live by reading prompts/cross_case_response.txt
    directly: its actual instructed format has never included the label, so
    a model correctly following the prompt's own worked examples produced
    citations this check could never match, making the fabrication check a
    silent no-op against the documented citation shape. The one historical
    "found live" example above happened to include the label (an incidental
    model choice on that run, not the instructed format), which is why the
    original regex looked correct against that one observation. Keep BOTH
    forms matched — a future model run adding the label back must not
    un-catch this either.

    `_check_leakage()` above cannot catch this — it returns early for
    cross-case queries, and it only inspects the chunk a [Document N] index
    points at, never the case-id text the model wrote next to it.

    Returns a list of issue strings (empty = every cited case id is real).
    """
    known: set[str] = set()
    for chunk in chunks:
        meta = chunk.get("metadata") or {}
        for key in ("case_id", "external_id"):
            value = meta.get(key)
            if value:
                known.add(str(value).strip().lower())

    # Live-caught false positive (KB1): this check exists specifically for
    # prompts/cross_case_response.txt's "[Document N, CASE-ID]" citation
    # shape, which only ever appears in case-linked cross-case answers —
    # those chunks virtually always carry a case_id/external_id, so `known`
    # is non-empty. A plain-RAG answer over a corpus with NO case-linkage
    # concept at all (e.g. the global legal-KB corpus, is_global=True,
    # category="legal_procedural_reference", no case_id on any chunk) has
    # no case ids to fabricate — a model citing "[Document 2, Section
    # 4(b)]" there is citing a statute clause, not inventing case
    # provenance, and `final_response.txt` never instructs the two-part
    # bracket format in the first place. Only run this check when the
    # chunk set actually has case ids to check against.
    if not known:
        return []

    issues: list[str] = []
    seen_bad: set[str] = set()
    for m in re.finditer(
        r"\[Document\s+\d+\s*,\s*(?:CASE-ID:\s*)?([^\]]+)\]", answer, re.IGNORECASE
    ):
        cited = m.group(1).strip()
        if not cited or cited.lower() in known or cited.lower() in seen_bad:
            continue
        seen_bad.add(cited.lower())
        logger.error(
            "FABRICATED CITATION: answer cites CASE-ID '%s', which does not "
            "appear in any retrieved chunk's metadata. Known ids: %s",
            cited,
            sorted(known)[:10] or "(none)",
        )
        issues.append(
            f"Citation references case '{cited}', which is not among the "
            "retrieved sources."
        )
    return issues


def _check_hedging(answer: str, chunks: list[dict]) -> list[str]:
    """
    Deterministic pre-check: for any chunk with confidence < 0.85 (via
    `_effective_confidence()` — legacy top-level `graph_confidence` OR
    the harness's `metadata.confidence`/`confidence_status`), verify that
    the answer contains a hedging phrase within _HEDGE_WINDOW characters
    of the [Document N] citation that references that chunk. Returns a
    list of issue strings (empty = hedging is adequate).

    [AMENDMENT — AGENT_HARNESS_DESIGN.md §7] `confidence_status ==
    "check_failed"` requires hedging UNCONDITIONALLY, regardless of what
    `confidence` itself holds (normally `None` for this status). This is
    the fix design §7 names explicitly: a chunk whose confidence
    computation raised must be treated at LEAST as cautiously as a
    known-low score — never as "no signal, proceed unhedged," which is
    the exact false-all-clear this check exists to prevent (the same
    shape of fix RESOLVED-5's `ConflictState` already applied one layer
    up, for a different field).
    """
    issues: list[str] = []
    for i, chunk in enumerate(chunks, start=1):
        conf, conf_status = _effective_confidence(chunk)
        meta = chunk.get("metadata") or {}
        # [AMENDMENT — Milestone D2, GRAPH_SCALE_SCHEMA_EXPANSION_PLAN.md]
        # graph_retriever.py's opened pending-SAME_AS traversal
        # (config.FEATURE_HEDGED_PENDING_TRAVERSAL) tags any chunk reached
        # only through an unconfirmed identity link with
        # `metadata.same_as_status == "pending"`. That chunk's numeric
        # confidence is already capped below 0.85 at the source (belt),
        # but this check now also keys on the tag directly (suspenders) —
        # an extension of this SAME function, not a second parallel
        # check, so a future change to how confidence compounds can never
        # silently stop requiring the disclosure this tag exists for.
        if meta.get("same_as_status") == "pending":
            needs_hedge, conf_label = True, "unconfirmed identity link (pending SAME_AS)"
        elif conf_status == "check_failed":
            needs_hedge, conf_label = True, "confidence check failed"
        elif conf is not None and conf < 0.85:
            needs_hedge, conf_label = True, f"graph_confidence={conf:.2f}"
        else:
            continue
        if not needs_hedge:
            continue

        # Find all occurrences of [Document N] in the answer
        pattern = rf"\[Document\s+{i}\]"
        for m in re.finditer(pattern, answer, re.IGNORECASE):
            pos = m.start()
            window_start = max(0, pos - _HEDGE_WINDOW)
            window_end = min(len(answer), pos + _HEDGE_WINDOW)
            window = answer[window_start:window_end].lower()
            if not any(phrase in window for phrase in _HEDGE_PHRASES):
                source = (chunk.get("metadata") or {}).get("source", f"chunk {i}")
                issues.append(
                    f"[Document {i}] (source: {source}, {conf_label}) "
                    f"is cited without a required hedging phrase nearby."
                )
                break  # one issue per chunk is enough
    return issues


# Confirmed live: the local generation model sometimes ignores the
# retrieved chunks entirely and answers as a generic assistant with "no
# database access" — a privacy/confidentiality refusal roleplay — even
# though the actual record was right there in its prompt (e.g. "I don't
# have access to specific case files, police records, or databases... this
# information is typically confidential... contact the appropriate law
# enforcement agency"). The LLM verifier judge is supposed to catch this
# under its own "off_topic" definition (a generic non-answer when
# case-specific content was available) but unreliably does — it marked
# this exact refusal grounded=true, off_topic=false and let it straight
# through to the user. A deterministic phrase check is a much more
# reliable backstop than relying on the same class of local model to
# correctly judge another local model's refusal.
# Two tiers, not one flat list — confirmed live this distinction matters:
# a genuinely good, cited answer ("Witness Name: Kamran...") got rejected
# because it ALSO included an honest closing caveat mentioning "not
# publicly available" (e.g. "further personal details are not publicly
# available beyond what's summarized here") — a legitimate hedge, not a
# refusal, but a flat phrase list can't tell those apart.
#
# _STRONG_REFUSAL_PHRASES: near-unambiguous even standing alone — an
# answer built around one of these IS the refusal, so these always flag
# regardless of whether a citation also happens to be present.
#
# _WEAK_REFUSAL_PHRASES: plausible as either a full refusal OR an honest
# caveat inside an otherwise-real answer — only flag these when the answer
# has NO [Document N] citation at all (i.e. it isn't using the evidence in
# the first place, so the phrase is load-bearing, not a hedge).
#
# Dropped entirely (confirmed live too broad even weak-tier): "contact the
# appropriate/relevant..." and "through the proper channels" — routine
# closing language on plenty of genuinely good answers, not a refusal
# signal at all.
_STRONG_REFUSAL_PHRASES = (
    "i don't have access to", "i do not have access to",
    "i don't have direct access to", "i do not have direct access to",
    "i'm not able to access", "i am not able to access",
    "i cannot provide information about specific",
    "no access to specific case files", "no access to police records",
)
_WEAK_REFUSAL_PHRASES = (
    "consult official police records", "typically confidential",
    "not publicly available", "closed legal or law enforcement investigation",
    "closed investigation", "part of an ongoing investigation and cannot",
)


def _check_refusal(answer: str) -> Optional[str]:
    """
    Deterministic pre-check: flag an answer that reads as a generic
    "I don't have access" / "consult the proper channels" refusal instead
    of actually using the provided chunks. Only meaningful when chunks were
    provided at all (verify_grounding already returns not-grounded outright
    for an empty chunk list) — a real refusal in the presence of real
    evidence is never correct, since the evaluator already confirmed
    relevance before this function is ever called.

    Returns an issue string, or None if no refusal pattern was found.
    """
    lowered = answer.lower()
    for phrase in _STRONG_REFUSAL_PHRASES:
        if phrase in lowered:
            return (
                f"Answer reads as a generic access/privacy refusal (contains "
                f"{phrase!r}) instead of using the provided evidence, which the "
                f"evaluator already confirmed was relevant."
            )

    # Weak-tier phrases are ambiguous on their own — plausible as either a
    # full refusal or an honest caveat inside an otherwise-real, cited
    # answer. Only load-bearing (and therefore worth flagging) when there's
    # no citation backing the rest of the answer up.
    if not _DOCUMENT_CITATION_RE.search(answer):
        for phrase in _WEAK_REFUSAL_PHRASES:
            if phrase in lowered:
                return (
                    f"Answer reads as a generic access/privacy refusal (contains "
                    f"{phrase!r}, and cites no [Document N] source) instead of "
                    f"using the provided evidence, which the evaluator already "
                    f"confirmed was relevant."
                )
    return None


# Minimum length before a citation-less answer is treated as suspicious —
# a short, honest "the documents don't contain X" (final_response.txt rule
# 3's own explicitly sanctioned wording for genuinely missing evidence) has
# nothing to cite and shouldn't be flagged; a long, substantive-looking
# answer with zero citations is a different, much more suspicious shape.
_SUBSTANTIAL_ANSWER_LEN = 150

# Confirmed live (2026-08-03, query_id 889): the generator sometimes writes
# "**Document 1**" (markdown bold, no brackets) instead of the prompted
# "[Document 1]" — a genuinely grounded, correctly-cited answer ("In
# Document 1, it is mentioned that 'the stolen items were recovered'...")
# then got rejected here as if it cited nothing at all, burning two
# regeneration attempts before falling back to a generic abstention. The
# check's actual purpose is "did this answer engage with a specific
# numbered source," not "did it use exact bracket syntax" — so the pattern
# now also accepts optional brackets/parens and optional markdown bold
# around "Document N", not just the bracketed form.
_DOCUMENT_CITATION_RE = re.compile(r"[\[(]?\*{0,2}Document\s+\d+\*{0,2}[\])]?", re.IGNORECASE)

# Confirmed live: a raw character-length gate is gameable by a compact
# script. An XGRAPH query asked in Urdu returned a numbered list of 8 names
# with zero [Document N] citations — a direct violation of
# cross_case_response.txt's mandatory "[Document N, CASE-ID]" citation rule
# (which, per that prompt's rule 9, applies regardless of what language the
# surrounding prose is written in). The whole list was 92 characters, well
# under _SUBSTANTIAL_ANSWER_LEN, so it skipped this check entirely, while
# the same underlying evidence asked in English (longer prose, same lack of
# citations) correctly tripped it. A numbered/bulleted list is itself the
# signal that matters, independent of raw length — each item is a discrete
# factual claim (a name, in this case) that needs its own citation, and a
# real "no information found" answer is never formatted as an enumerated
# list of specifics.
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+\S", re.MULTILINE)
_MIN_LIST_ITEMS_FOR_SUBSTANTIAL = 2


def _check_no_citation(answer: str) -> Optional[str]:
    """
    Deterministic pre-check, complementary to _check_refusal: catches any
    phrasing of "I won't/can't use the evidence" without relying on a fixed
    phrase list, which is inherently incomplete — confirmed live, the local
    model produces new refusal wordings ("not publicly available", "part of
    a closed... investigation", etc.) faster than a phrase list can be kept
    current. final_response.txt's own rule 2 requires citing every claim as
    [Document N]; a substantive-looking answer with zero such citations,
    for a query the evaluator already confirmed relevant chunks exist for,
    is almost never actually using the evidence — a real grounded answer
    of any length past a one-liner cites something.

    "Substantial" is judged two ways, either one is enough: raw length past
    _SUBSTANTIAL_ANSWER_LEN, or a numbered/bulleted list with at least
    _MIN_LIST_ITEMS_FOR_SUBSTANTIAL items (see _LIST_ITEM_RE's comment for
    why length alone isn't a reliable proxy across scripts).

    Returns an issue string, or None if the answer looks properly cited (or
    is short enough / unstructured enough to plausibly be a legitimate
    no-citation "not found").
    """
    is_substantial = len(answer) >= _SUBSTANTIAL_ANSWER_LEN
    if not is_substantial:
        is_substantial = len(_LIST_ITEM_RE.findall(answer)) >= _MIN_LIST_ITEMS_FOR_SUBSTANTIAL
    if not is_substantial:
        return None
    if _DOCUMENT_CITATION_RE.search(answer):
        return None
    return (
        "Answer is substantial (long, or a multi-item list) but cites no "
        "[Document N] source at all, despite the evaluator already "
        "confirming relevant chunks exist — reads as avoiding the provided "
        "evidence rather than using it."
    )


# ============================================================
# [Gold-QA fix — Module 101] ATTRIBUTION BY SOURCE NAME
#
# THE DEFECT. `_check_no_citation()` above rejects a substantial answer that
# carries no `[Document N]` token. On KB9 that fires on an answer which is
# CORRECT and which the LLM judge has just cleared claim by claim
# (`grounded: true`, `unsupported_claims: []`): it states CrPC s.174/s.176
# and Punjab Police Rules 25.31, states gold's own figure — "10 of the 73
# FIR(s) that carry a recorded section cite PPC §302" — and states the
# schema gap honestly. Its only fault is HOW it attributes: it names its
# sources instead of numbering them —
#
#   "According to the **Code of Criminal Procedure (Pakistan)** …"
#   "According to **our own case records (cross-case aggregate)** …"
#
# Both of those strings are the `source` / `source_file` labels of chunks
# in this very window (chunks 1/3/4 and the composed data-half chunk 7).
# The answer is not avoiding the evidence; it is citing it by name.
# `refusal_issue` then sets `off_topic=True`, and the sub-agent's
# [PRESERVE] contract discards the whole answer.
#
# Module 71 §8 filed this same check firing on Meta-Analysis' G6 synthesis
# (10 of 12 runs) and Module 82 §8d recorded it firing there unforced on
# shipped code. Module 71's own prescription was "make the [Document N]
# markers unnecessary by carrying provenance out of band" — which is what
# this is, on the Semantic Search side.
#
# WHY THIS IS NOT A LOOSENING. The exemption below needs BOTH of two
# independent things to be true, and each alone is deliberately not enough:
#
#   (a) the answer contains, verbatim, the normalised source label of a
#       chunk it was actually given — a deterministic string test against
#       THIS window's own metadata, not a similarity judgement; and
#   (b) the LLM judge independently cleared every claim in the answer
#       (grounded, not off-topic, no unsupported claims), with no leakage
#       and no other deterministic pre-check outstanding.
#
# So an evasive answer still fails (a): it names no source it was handed.
# A fluent fabrication still fails (b): the judge flags it, exactly as it
# flagged Module 82's forced `rule 27.41(3)` control 3 of 3 — and that
# control's fabrication carries `[Document 2]`/`[Document 7]` markers, so
# this check never adjudicated it in the first place. A genuine refusal is
# untouched: `_check_refusal()` is evaluated separately below and is NEVER
# exempted.
#
# What IS lost is the `[Document N]` marker itself, so the exemption is not
# silent — `citation_format_degraded` is returned to the caller, which
# caveats the answer.
#
# The `source` label of a chunk that is a bare ingest filename carries a
# leading corpus index and an extension ("1_1898_Code_of_Criminal_
# Procedure_(Pakistan).pdf"), neither of which any answer will ever write,
# so both are stripped before matching. A label too short or too generic to
# be evidence of anything ("unknown", "entity_graph") is refused outright by
# the token/length floor: matching one of those would make the exemption
# trivially satisfiable.
CITATION_FORMAT_DEGRADED_KEY = "citation_format_degraded"

# The caveat a caller MUST surface when it serves an exempted answer. Lives
# here, next to the flag, so the six other agents that call
# `verify_grounding()` can reuse the exact wording rather than each inventing
# one -- Meta-Analysis' own wiring is another track's file and is filed, not
# done here (see MODULE101_RESULT.md section 8).
CITATION_FORMAT_DEGRADED_CAVEAT = (
    "This answer attributes its sources by name rather than with [Document N] "
    "markers, so individual claims cannot be traced to a specific source; every "
    "claim was still checked against the retrieved evidence."
)

_SOURCE_LABEL_MIN_TOKENS = 3
_SOURCE_LABEL_MIN_CHARS = 12
_SOURCE_LABEL_SEPARATORS_RE = re.compile(r"[_\-]+")
_SOURCE_LABEL_WS_RE = re.compile(r"\s+")
_SOURCE_LABEL_EXT_RE = re.compile(r"\.(pdf|txt|docx?|csv|json|md)$", re.IGNORECASE)


def _normalise_source_label(raw: Optional[str]) -> Optional[str]:
    """A chunk's source label reduced to the form an answer would write it.

    Extension dropped, `_`/`-` folded to spaces, whitespace collapsed,
    lower-cased, and any LEADING all-digit tokens (the corpus index and a
    statute year that prefixes the filename, "1_1898_…") removed — an
    answer writes "Code of Criminal Procedure (Pakistan)", never
    "1 1898 Code of Criminal Procedure (Pakistan)".

    Returns None for a label too short or too few-worded to be evidence
    that the answer engaged with THIS window rather than with general
    knowledge.
    """
    if not raw or not raw.strip():
        return None
    text = _SOURCE_LABEL_EXT_RE.sub("", raw.strip())
    text = _SOURCE_LABEL_SEPARATORS_RE.sub(" ", text)
    text = _SOURCE_LABEL_WS_RE.sub(" ", text).strip().lower()
    tokens = text.split(" ")
    while tokens and tokens[0].isdigit():
        tokens.pop(0)
    if len(tokens) < _SOURCE_LABEL_MIN_TOKENS:
        return None
    label = " ".join(tokens)
    if len(label) < _SOURCE_LABEL_MIN_CHARS:
        return None
    return label


def _answer_names_a_cited_source(answer: str, chunks: list[dict]) -> Optional[str]:
    """The first cited chunk's source label the answer states verbatim, or None.

    The answer is normalised with the SAME transform as the label (minus the
    leading-digit strip, which is a property of a filename and not of prose),
    so "Punjab Police Rules-III" in the answer matches
    "4_Punjab-Police-Rules-III.pdf" in the metadata.
    """
    if not answer:
        return None
    hay = _SOURCE_LABEL_WS_RE.sub(
        " ", _SOURCE_LABEL_SEPARATORS_RE.sub(" ", answer)
    ).lower()
    for chunk in chunks:
        meta = chunk.get("metadata") or {}
        for raw in (chunk.get("source_file"), meta.get("source"), meta.get("source_file")):
            label = _normalise_source_label(raw)
            if label and label in hay:
                return label
    return None


async def verify_grounding(
    answer: str,
    cited_chunks: list[dict],
    case_id: Optional[str],
    cross_case_ids: Optional[list[str]] = None,
    target_date: Optional[int] = None,
) -> dict:
    """
    Verify that a generated answer is grounded in its cited source chunks.

    This is the hard gate between generation and delivery.

    Args:
        answer:           The complete generated answer text.
        cited_chunks:     The chunks that were retrieved and given to the
                          generator (same list used to build the prompt).
        case_id:          The active case for this query, or None / "cross_case"
                          for XGRAPH/XAGG routes.
        cross_case_ids:   Case IDs explicitly allowed for cross-case routes.
        target_date:      Query's target year as int (e.g. 2025) for temporal
                          validity checks. None to skip.

    Returns:
        Dict with keys:
            "grounded"          (bool)
            "off_topic"         (bool)
            "leaked_case_id"    (str | None)
            "unsupported_claims" (list[str])
            "reason"            (str)
    """
    # [Scenario-test Finding G] An EMPTY answer must never pass this gate.
    # `call_llm()` can return empty content without raising (a local-model
    # empty response whose cloud fallback also yields nothing), so callers'
    # try/except around generation doesn't catch it. Before this guard, an
    # empty string reached the LLM verifier, which reasonably concluded
    # "the answer is empty and contains no claims to verify" and returned
    # grounded=True — so a 0-char answer was delivered to the user as a
    # verified success, with no error surfaced anywhere. Confirmed live on
    # the XAGG/Large-Scale-Aggregate route.
    #
    # This is deliberately fixed HERE rather than in each sub-agent: six of
    # the eleven agents call verify_grounding() with no empty-answer guard of
    # their own, and a future agent would inherit the same trap. Agents that
    # have a meaningful fallback (e.g. large_scale_aggregate's raw computed
    # aggregate) now correctly route into it via the not-grounded branch they
    # already have.
    if not answer or not answer.strip():
        logger.warning("Verifier received an empty answer — returning not grounded.")
        return {
            "grounded": False,
            "off_topic": False,
            "leaked_case_id": None,
            "unsupported_claims": [],
            "reason": "The generated answer was empty; nothing to verify or serve.",
            "refusal_detected": False,
        }

    if not cited_chunks:
        logger.warning("Verifier received empty chunk list — returning not grounded.")
        return {
            "grounded": False,
            "off_topic": False,
            "leaked_case_id": None,
            "unsupported_claims": [],
            "reason": "No source chunks were provided; cannot verify grounding.",
            "refusal_detected": False,
        }

    # ── Deterministic pre-checks (fast, no LLM) ──────────────────────────
    temporal_issues = _check_temporal(cited_chunks, target_date)
    leaked_case = _check_leakage(answer, cited_chunks, case_id, cross_case_ids)
    hedging_issues = _check_hedging(answer, cited_chunks)
    # [Module 101] Split, so the two can be merged on different terms below.
    # `_check_refusal()` is NEVER exempted; `_check_no_citation()` is, and
    # only under the two-part test in this module's comment block above.
    refusal_issue = _check_refusal(answer)
    no_citation_issue = _check_no_citation(answer)
    # [Scenario-test Finding J] Catch invented CASE-IDs inside citations —
    # see _check_fabricated_case_ids()'s own docstring for why the leakage
    # check above cannot cover this.
    fabricated_issues = _check_fabricated_case_ids(answer, cited_chunks)

    pre_check_issues: list[str] = temporal_issues + hedging_issues + fabricated_issues
    # [Module 101] `no_citation_issue` still counts here, unchanged: Module
    # 61's exhaustive-negative override must stay off whenever ANY
    # deterministic check has an outstanding finding, and whether this one is
    # later exempted is not known until the judge has answered.
    pre_check_failed = bool(
        pre_check_issues or leaked_case or refusal_issue or no_citation_issue
    )

    # ── Build LLM judge input ─────────────────────────────────────────────
    chunks_text = _format_chunks_for_verifier(cited_chunks)
    active_case_str = case_id or "cross_case"
    allowed_str = ", ".join(cross_case_ids) if cross_case_ids else "(none)"

    user_input = (
        f"ANSWER:\n{answer}\n\n"
        f"CHUNKS:\n{chunks_text}\n"
        f"ACTIVE_CASE_ID: {active_case_str}\n"
        f"ALLOWED_CASE_IDS: [{allowed_str}]"
    )

    # ── LLM judge ──────────────────────────────────────────────────────
    # call_llm_json retries with an explicit schema-naming correction if
    # Qwen3 answers conversationally instead of with JSON (confirmed live —
    # the identical failure shape already fixed in router.py/evaluator.py),
    # then makes one guaranteed cloud attempt before giving up, rather than
    # fail-closing on the first sign of local-model trouble.
    llm_result, raw = await call_llm_json(
        system_prompt=_SYSTEM_PROMPT,
        user_message=user_input,
        temperature=0.0,
        # Qwen3-14B's thinking trace consumes max_tokens before its JSON
        # answer. 800 was confirmed live to be too tight for a real
        # verification prompt (multiple retrieved chunks, several
        # unsupported_claims): the model's own JSON got cut off
        # mid-string ("Unterminated string...") rather than just
        # running out of room for the thinking trace. 2000 gives real
        # headroom for both on realistically-sized input. Module 6.3:
        # the cloud fallback (Groq/Gemini) has no equivalent
        # thinking-trace tax, so it keeps the original, cheaper
        # 800-token budget — only the local branch needs the extra room.
        max_tokens=2000,
        cloud_max_tokens=800,
        role="reasoning",
        validate=lambda r: isinstance(r, dict) and "grounded" in r and "reason" in r,
        schema_hint='"grounded" (true/false), "off_topic" (true/false), "leaked_case_id" (string or null), "unsupported_claims" (array of strings), "reason" (string)',
        _call_llm=call_llm,
    )

    if llm_result is None:
        logger.error(
            "Verifier failed to return valid JSON after retries. Raw: %s. "
            "Defaulting to not grounded (fail-closed).",
            raw[:150],
        )
        llm_result = {
            "grounded": False,
            "off_topic": False,
            "leaked_case_id": None,
            "unsupported_claims": [],
            "reason": "Verifier failed to parse — defaulting to not grounded (fail-closed).",
            "refusal_detected": False,
        }

    # ── [Module 61] Negative inference over a COMPLETE listing ────────────
    # The judge may still reject a correct negative ("65/26 has none")
    # despite rule 7, because it is a sampled verdict — that sampling is
    # exactly what made CR3 pass or fail on a coin flip across Module 57's
    # 14 runs. When EVERY claim it flagged is an absence claim whose
    # subject is deterministically confirmed missing from a chunk the
    # CALLER declared exhaustive, the rejection is overturned here, in
    # Python, so the outcome is reproducible rather than resampled.
    #
    # Deliberately conservative — the override does not run at all if the
    # judge found ANY other problem (off_topic, a claim that is not
    # absence-shaped, a claim naming an identifier the listing actually
    # contains), and the deterministic pre-checks below still overrule it.
    exhaustive_texts = exhaustive_chunk_texts(cited_chunks)
    if (
        exhaustive_texts
        and not llm_result.get("grounded", False)
        and not llm_result.get("off_topic", False)
        and not llm_result.get("leaked_case_id")
        and not pre_check_failed
    ):
        flagged = [str(c) for c in (llm_result.get("unsupported_claims") or []) if str(c).strip()]
        # A rejection with no itemised claim gives nothing to check, so it
        # stands; the reason line is used as the single claim in that case
        # only when it is itself absence-shaped and identifier-bearing.
        candidates = flagged or [str(llm_result.get("reason") or "")]
        if candidates and all(
            negative_claim_is_supported_by_exhaustive_listing(
                c, listings_a_claim_is_about(c, cited_chunks)
            )
            for c in candidates
        ):
            logger.info(
                "Verifier [Module 61]: overturning rejection — every flagged claim "
                "is a negative over a COMPLETE listing whose subject is confirmed "
                "absent. Claims: %s",
                "; ".join(c[:80] for c in candidates),
            )
            llm_result["grounded"] = True
            llm_result["unsupported_claims"] = []
            llm_result["exhaustive_negative_override"] = True
            llm_result["reason"] = (
                "Supported: the only claims flagged were negative inferences over a "
                "complete listing, and each named record is confirmed absent from "
                "that listing."
            )

    # ── Merge deterministic findings into LLM result ──────────────────────
    if pre_check_issues:
        existing = llm_result.get("unsupported_claims") or []
        llm_result["unsupported_claims"] = existing + pre_check_issues
        llm_result["grounded"] = False
        if not llm_result.get("reason") or llm_result["reason"].startswith("All claims"):
            llm_result["reason"] = pre_check_issues[0]

    if leaked_case:
        llm_result["leaked_case_id"] = leaked_case
        llm_result["grounded"] = False
        llm_result["reason"] = (
            f"Cross-case evidence leakage detected: chunk from case '{leaked_case}' "
            f"cited in answer for case '{active_case_str}'."
        )

    # ── [Module 101] Attribution by source name ──────────────────────────
    # Evaluated HERE, after the merges above, so condition (b) sees the
    # judge's verdict as it actually stands — including any deterministic
    # finding or leakage that has just overruled it.
    if no_citation_issue:
        named_source = _answer_names_a_cited_source(answer, cited_chunks)
        judge_cleared = (
            bool(llm_result.get("grounded"))
            and not llm_result.get("off_topic")
            and not (llm_result.get("unsupported_claims") or [])
            and not llm_result.get("leaked_case_id")
            and not refusal_issue
        )
        if named_source and judge_cleared:
            logger.info(
                "Verifier [Module 101]: answer carries no [Document N] marker but "
                "names cited source %r verbatim, and the judge cleared every "
                "claim — serving it with a citation-format caveat rather than "
                "discarding it.",
                named_source,
            )
            llm_result[CITATION_FORMAT_DEGRADED_KEY] = True
            llm_result["named_source"] = named_source
        else:
            # Not exempted — restore the pre-Module-101 behaviour exactly.
            refusal_issue = refusal_issue or no_citation_issue

    # Distinct from off_topic/grounded — callers use this to decide whether
    # regenerating with a corrective prompt is worth trying (a refusal is a
    # different, likely-fixable failure from "the evidence genuinely
    # doesn't answer the question," where regenerating would just produce
    # the same non-answer again).
    llm_result["refusal_detected"] = bool(refusal_issue)

    if refusal_issue:
        llm_result["off_topic"] = True
        llm_result["grounded"] = False
        llm_result["reason"] = refusal_issue

    logger.info(
        "Verifier: grounded=%s off_topic=%s leaked=%s unsupported=%d — %s",
        llm_result.get("grounded"),
        llm_result.get("off_topic"),
        llm_result.get("leaked_case_id"),
        len(llm_result.get("unsupported_claims") or []),
        (llm_result.get("reason") or "")[:80],
    )
    return llm_result


# [Gold-QA fix — ROOT_CAUSE_AND_FIXES.md Module 4] `verify_grounding()`
# above is tuned for free-text claims grounded in narrative document
# chunks — its LLM judge, hedging checks, and citation-format checks all
# assume the "source" is prose a human wrote. large_scale_aggregate.py and
# cross_case_linkage.py hand it something structurally different: an NL
# paraphrase of a DETERMINISTIC, code-computed result (a count, a list of
# recurring entities) — the numbers are correct by construction, they came
# from the query, not an LLM claim. Live-confirmed (Gold-QA report §2.4):
# applying the strict free-text judge to this shape rejected accurate
# paraphrases often enough that the raw-aggregate fallback ("Entity-graph
# search found connections across 6 cases, chain confidence 50%...")
# became the DE FACTO primary answer instead of the safety fallback it was
# designed to be.
#
# This does not touch verify_grounding()'s own behavior or its narrative
# call sites (RAG/GRAPH/SQL/WEB/XGRAPH/XNETWORK) at all — it is a
# separate, deliberately narrower check for this one call shape:
#   1. Still runs the two SECURITY-relevant deterministic checks
#      (cross-case leakage, fabricated case IDs) unconditionally — those
#      are about what the answer CLAIMS, not about narrative-vs-structured
#      source shape, and must never be relaxed.
#   2. Replaces the free-text LLM judge with a deterministic numeric-
#      consistency check: every number the paraphrase states must appear
#      somewhere in the structured source text it was paraphrasing. A
#      paraphrase that invents a number not present in the computed result
#      fails; one that only restates the real numbers in prose passes,
#      regardless of hedging phrasing or citation-tag conventions that
#      make sense for a narrative document but not for a one-shot computed
#      summary.
#
# [Gold-QA fix — GOLD_QA_MASTER_FIX_PLAN.md Module 17, RC-5] The digit-run
# regex below used to stop at the decimal point, so "15.0" tokenized as two
# SEPARATE numbers, "15" and "0". A source that computed an average as
# "1401.34" and a paraphrase that (correctly) rounded it to "1401.3" then
# split into "1401"/"3" vs "1401"/"34" — "3" is not "34", so a verbatim
# restatement of the source's own computed average got flagged as an
# invented number purely from a decimal-rounding difference, not an actual
# fabrication. Live-confirmed shape: M7's "2024 ka ausat 15.0 minute...
# 2026 ka ausat 1401.3 minute" reporting-delay comparison. Matching the
# decimal tail keeps "15.0" and "1401.3" as single tokens, compared as
# whole numbers the way an investigator actually reads them.
_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
# Strip [Document N] / [Document N, CASE-ID] citation markers before
# extracting numbers from the ANSWER (never from source_text) — the
# citation index itself ("1" in "[Document 1]") and any digits inside a
# cited case id are provenance formatting, not a claimed figure, and must
# not be checked against the computed source text the way a genuine
# number in the prose is.
_CITATION_MARKER_RE = re.compile(r"\[Document\s+\d+(?:\s*,[^\]]*)?\]", re.IGNORECASE)


def _numbers_in(text: str, *, strip_citations: bool = False) -> set[str]:
    """Every digit run in `text` (decimal point included), comma separators
    stripped, as a set of canonical strings — "1,234" and "1234" compare
    equal, "15.0" stays one token rather than splitting at the decimal
    point, order and duplicates don't matter for a subset check."""
    if strip_citations:
        text = _CITATION_MARKER_RE.sub("", text or "")
    return {m.group(0).replace(",", "") for m in _NUMBER_RE.finditer(text or "")}


# [Gold-QA fix — Module 17, RC-5] A paraphrase of a rate/ratio aggregate
# (Module 13's rate primitive — e.g. "9 of 73 FIRs (~12%)") legitimately
# states a PERCENTAGE that is the exact arithmetic result of two counts the
# source already states — same shape as Module 4's grand-total carve-out,
# generalized from addition to division. "9" and "73" are already literal
# matches; only the derived "12" is new, and it is not invented — it is
# `round(100 * 9 / 73) == 12`. Narrow on purpose, mirroring the grand-total
# check's own discipline (ROOT_CAUSE_AND_FIXES.md Module 4 / the tests
# above): only an EXACT round() result at the candidate's own precision
# counts as supported, never a nearby approximation — a genuinely fabricated
# number a few points off a real ratio must still fail.
def _is_derived_ratio(candidate: str, source_values: set[str]) -> bool:
    """True if `candidate` equals round(100*a/b, ...) or round(a/b, ...)
    for some pair of DISTINCT numeric values already present in
    `source_values`, rounded to the same number of decimal places the
    candidate itself is stated with (0 decimals for a bare integer)."""
    try:
        cand_val = float(candidate)
    except ValueError:
        return False
    places = len(candidate.split(".", 1)[1]) if "." in candidate else 0

    parsed: list[float] = []
    for s in source_values:
        try:
            parsed.append(float(s))
        except ValueError:
            continue

    for a in parsed:
        for b in parsed:
            if a == b or b == 0:
                continue
            if round(100 * a / b, places) == cand_val:
                return True
            if round(a / b, places) == cand_val:
                return True
    return False


async def verify_structured_aggregate_paraphrase(
    answer: str,
    source_text: str,
    case_id: Optional[str],
    cross_case_ids: Optional[list[str]] = None,
) -> dict:
    """
    Relaxed grounding check for an NL paraphrase of a deterministic,
    code-computed aggregate/cluster result — see this module-level comment
    block above for the full rationale. `source_text` is the raw
    computed-result text the paraphrase was generated from (e.g.
    `XAggToolResult.raw_summary_text`), NOT a retrieved document chunk.

    Returns the same shape as `verify_grounding()` so callers (
    large_scale_aggregate.py, cross_case_linkage.py) need no change beyond
    swapping which function they call.
    """
    if not answer or not answer.strip():
        return {
            "grounded": False, "off_topic": False, "leaked_case_id": None,
            "unsupported_claims": [], "reason": "The generated answer was empty; nothing to verify or serve.",
            "refusal_detected": False,
        }
    if not source_text or not source_text.strip():
        return {
            "grounded": False, "off_topic": False, "leaked_case_id": None,
            "unsupported_claims": [], "reason": "No computed source text was provided; cannot verify grounding.",
            "refusal_detected": False,
        }

    # Reuse the existing deterministic security checks — [Document N]-style
    # citations still appear in these paraphrases (the generation prompt
    # asks for them same as any other route), so _check_leakage's citation
    # scan still applies; it needs a chunk list, so wrap source_text as a
    # single synthetic chunk the same way the two call sites already do
    # for verify_grounding(). _check_fabricated_case_ids additionally needs
    # every case id the paraphrase is LEGITIMATELY allowed to cite present
    # in some chunk's metadata — a cross-case aggregate paraphrase routinely
    # names several real case ids from `cross_case_ids` (e.g. the recurring
    # entity's own case list), so one synthetic chunk per allowed id, not
    # just the single "computed-aggregate" placeholder.
    synthetic_chunks = [{"id": "computed-aggregate", "text": source_text, "metadata": {"case_id": case_id if case_id != "cross_case" else None}}]
    synthetic_chunks += [
        {"id": f"computed-aggregate-{cid}", "text": "", "metadata": {"case_id": cid}}
        for cid in (cross_case_ids or [])
    ]
    leaked_case = _check_leakage(answer, synthetic_chunks, case_id, cross_case_ids)
    fabricated_issues = _check_fabricated_case_ids(answer, synthetic_chunks)

    # [Gold-QA fix — Module 4, question D1] A paraphrase legitimately states
    # a TOTAL that is the sum of the source's own per-category counts — e.g.
    # the source lists "PPC: 25 cases, Arms Ordinance 1965: 21 cases, …" and
    # the answer correctly says "79 FIRs in total". That total is not
    # literally present in the source, so a bare set-difference wrongly
    # flags it as an invented number and triggers the raw-aggregate
    # fallback instead of stating the plain count.
    #
    # Narrowed on purpose: only the FIRST breakdown group in the source is
    # summed — `_render_aggregate_text()`'s "Breakdown by individual legal
    # code" section (when present) is a SEPARATE, OVERLAPPING per-act
    # breakdown (a case can carry more than one act), never a partition of
    # the total, so summing past that marker would produce a number that
    # looks plausible but is not the real total and must never be treated
    # as a legitimate grand total. Extracting the per-category COUNTS
    # specifically (numbers immediately followed by "case"/"cases"/"FIR"/
    # "FIRs"/"record"/"records"), not every number, also keeps a source
    # containing years like "1965"/"2016" inside statute names out of the
    # sum.
    first_breakdown = (source_text or "").split("Breakdown by individual legal code")[0]
    src_nums = _numbers_in(source_text)
    ans_nums = _numbers_in(answer, strip_citations=True)
    _count_matches = re.findall(
        r"(\d[\d,]*)\s*(?:cases?|firs?|records?)\b", first_breakdown, re.IGNORECASE
    )
    _count_total = sum(int(c.replace(",", "")) for c in _count_matches) if _count_matches else None
    unsupported_numbers = sorted(
        n for n in (ans_nums - src_nums)
        if not (_count_total is not None and n.isdigit() and int(n) == _count_total)
        # [Module 17, RC-5] ...or a rate/ratio (percentage or fraction)
        # derived, by exact arithmetic, from two counts the source already
        # states — see _is_derived_ratio()'s own docstring for why this is
        # narrow, not a fuzzy-match relaxation.
        and not _is_derived_ratio(n, src_nums)
    )

    grounded = not leaked_case and not fabricated_issues and not unsupported_numbers
    reason = "Paraphrase numbers match the computed source; deterministic check passed."
    if leaked_case:
        reason = f"Cross-case evidence leakage detected: source from case '{leaked_case}' cited outside its allowed scope."
    elif fabricated_issues:
        reason = fabricated_issues[0]
    elif unsupported_numbers:
        reason = f"Paraphrase states number(s) not present in the computed result: {', '.join(unsupported_numbers)}."

    logger.info(
        "Structured-aggregate verifier: grounded=%s leaked=%s unsupported_numbers=%s — %s",
        grounded, leaked_case, unsupported_numbers, reason[:80],
    )
    return {
        "grounded": grounded,
        "off_topic": False,
        "leaked_case_id": leaked_case,
        "unsupported_claims": fabricated_issues + [f"number: {n}" for n in unsupported_numbers],
        "reason": reason,
        "refusal_detected": False,
    }
