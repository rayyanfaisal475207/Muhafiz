# ============================================================
# Schema-absence claim classifier — [Gold-QA fix — Module 151]
#
# PURPOSE:
# Given the claims the grounding judge flagged as unsupported, decide for
# each one whether it is a claim about the SHAPE of the platform's records
# ("there is no field for X") — the only kind of claim the record
# inventory can ground — as opposed to a claim about their CONTENTS ("no
# FIR mentions X", "FIR 64/26 is absent from the listing"), a POSITIVE
# claim ("the system does record X"), or anything else.
#
# WHY A CLASSIFICATION AND NOT A KEYWORD MATCH (MODULE151_RESULT.md §2):
# the two absence kinds share their vocabulary. "case records lacking
# data on inquest report compliance" (KB9, a true schema absence) and
# "fir-64-26 does not appear in the linkage list" (CR3 run 2, a
# fabricated data negative Module 82 §4c recorded the verifier catching)
# both say something is "not there". What separates them is whether the
# missing thing is a FIELD or a VALUE, and that is a reading of the
# sentence, not a token in it. Module 143 made the same call for the
# evaluator's reason text and measured it on a held-out set; this module
# does the same (`scripts/module151_classifier_offline.py`).
#
# WHY THE INVENTORY IS SHOWN TO THE CLASSIFIER: it is asked to name any
# existing field that already serves the concept (`matching_fields`). The
# verifier refutes the claim if EITHER the classifier names one OR
# `schema_inventory.fields_serving()` finds one by keyword. Two
# independent readers must both find nothing before an absence counts as
# confirmed — a fabricated schema absence has to get past both.
#
# FAIL-CLOSED: every failure path (exception, malformed JSON, a claim the
# model did not return, an unknown kind) yields "not a confirmable schema
# absence", and the judge's rejection stands. This is the OPPOSITE of
# Module 143's retry gate, and deliberately so: there, the safe default
# was to keep retrying; here, the safe default is to keep rejecting.
# ============================================================

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from src.llm.client import call_llm
from src.pipeline.json_extract import call_llm_json
from src.pipeline.schema_inventory import RecordInventory, fields_serving

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "schema_claim.txt"
_SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")

KIND_SCHEMA_ABSENCE = "schema_absence"
KIND_DATA_ABSENCE = "data_absence"
KIND_SCHEMA_PRESENCE = "schema_presence"
KIND_OTHER = "other"
_KINDS = frozenset({KIND_SCHEMA_ABSENCE, KIND_DATA_ABSENCE, KIND_SCHEMA_PRESENCE, KIND_OTHER})

# Bound on how much of the answer is shown as context. The flagged claims
# are the unit; the answer only helps resolve scope.
_ANSWER_CONTEXT_CHARS = 4000

# ── The sentence guard — deterministic, and the reason the classifier's
# verdict alone is never enough (MODULE151_RESULT.md §3.2, "cut 2") ──────
#
# Measured on the held-out set with the real model: the classifier called
# "in none of the 8 women-violence cases was the chain of custody
# maintained", "none of the 32 recovered weapons was photographed" and "no
# zimni entry records an arrest" schema absences — all three are claims
# about EVENTS or CONTENTS, and none has a column, so the inventory
# "confirmed" them — and it called two POSITIVE claims ("our weapon register
# records packaging and photographs") schema absences too. A 14B reading of
# a sentence does not separate "there is no field for X" from "X did not
# happen" or from "X is recorded" with a usable margin. So, exactly as
# Module 61 requires every absence phrase to resolve to a named identifier
# before it trusts the judge's wording, this guard requires the ANSWER
# SENTENCE the claim is about — quoted verbatim by the classifier, and
# verified to be in the answer — to (1) contain a NEGATION and (2) speak of
# a FIELD / column / khana / schema / what the records track, capture or
# store; and it requires the same two things of the judge's own flagged
# claim once its evidence wrapper ("…is not supported by any chunk", "— no
# chunk discusses…") is stripped, so that a positive claim the classifier
# mislabels cannot be rescued by quoting some other, negative sentence of
# the same answer. The judge's wording cannot carry the test un-stripped:
# every claim it writes ends in a negation about the EVIDENCE.
#
# Deliberately NOT required: lexical overlap between the judge's claim and
# the quoted sentence. Measured on KB5's shape, the judge summarises
# ("Claims about missing data fields in case records…") while the answer
# names the concept ("no field for whether chain of custody was
# maintained"), and the two share no content word at all.
#
# "record(s)" as a verb is deliberately NOT schema vocabulary: "no zimni
# entry records an arrest" is the data-negative shape, and "no record of X"
# is a claim about contents.
_NEGATION_RE = re.compile(
    r"\b(?:no|not|none|nowhere|never|lacks?|lacking|missing|absent|absence|without)\b"
    r"|n't\b"
    r"|\b(?:nahi|nahin|nahee|nai)\b"
    r"|نہیں|کوئی|بغیر|\bنہ\b",
    re.IGNORECASE,
)
_SCHEMA_VOCAB_RE = re.compile(
    r"\b(?:fields?|columns?|schema|tables?|record[- ]types?|data[- ]points?|attributes?)\b"
    r"|\b(?:tracks?|tracked|tracking|captures?|captured|capturing|stores?|stored|storing)\b"
    r"|\b(?:khana|khaana)\b"
    r"|خانہ|خانے|فیلڈ|کالم|سکیما",
    re.IGNORECASE,
)
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
_MIN_QUOTE_CHARS = 20

# The judge's evidence wrapper, stripped before the claim itself is tested.
# Everything from the first dash-clause or from the first evidence phrase
# ("is not supported…", "not stated in…", "no chunk…", "unsupported…") to
# the end is the judge talking about the chunks, not the answer's claim.
_CLAIM_WRAPPER_RE = re.compile(
    r"\s+[—–-]+\s+.*$"
    r"|\b(?:is|are|was|were)?\s*(?:not|un)\s*supported\b.*$"
    r"|\b(?:is|are)\s+unsupported\b.*$"
    r"|\bnot\s+(?:explicitly\s+|directly\s+)?(?:stated|mentioned|found|present|supported|addressed|confirmed|in\s+any)\b.*$"
    r"|\bno\s+(?:chunk|document|source)\b.*$"
    r"|\b(?:which|but|though|as|however)\s+(?:the\s+)?(?:chunks?|documents?)\b.*$"
    r"|\bkisi\s+(?:chunk|document)\b.*$"
    r"|کسی\s+(?:دستاویز|چنک).*$",
    re.IGNORECASE,
)


def _squash(text: str) -> str:
    return "".join(_WORD_RE.findall((text or "").lower()))


def claim_core(claim: str) -> str:
    """The judge's flagged claim with its evidence wrapper removed."""
    return _CLAIM_WRAPPER_RE.sub("", (claim or "").strip()).strip()


def sentence_guard(claim: str, answer_sentence: Optional[str], answer: str) -> Optional[str]:
    """None when the judge's claim and the quoted answer sentence both pass
    every test above; otherwise the name of the first test failed."""
    core = claim_core(claim)
    if not _NEGATION_RE.search(core):
        return "claim_is_not_negative"
    if not _SCHEMA_VOCAB_RE.search(core):
        return "claim_has_no_schema_vocabulary"
    quote = (answer_sentence or "").strip()
    if len(_squash(quote)) < _MIN_QUOTE_CHARS or _squash(quote) not in _squash(answer):
        return "quote_not_in_answer"
    if not _NEGATION_RE.search(quote):
        return "no_negation_in_sentence"
    if not _SCHEMA_VOCAB_RE.search(quote):
        return "no_schema_vocabulary_in_sentence"
    return None


@dataclass
class ClassifiedClaim:
    claim: str
    kind: str
    scope: Optional[str] = None
    concept: str = ""
    field_keywords: list[str] = field(default_factory=list)
    matching_fields: list[str] = field(default_factory=list)
    reason: str = ""
    answer_sentence: str = ""
    # Filled in by `ground_against_inventory()`:
    keyword_hits: Optional[list[str]] = None
    confirmed_absent: bool = False
    verdict: str = "not_schema"

    def as_log(self) -> str:
        return (
            f"kind={self.kind} scope={self.scope} concept={self.concept!r} "
            f"keywords={self.field_keywords} llm_matches={self.matching_fields} "
            f"keyword_hits={self.keyword_hits} sentence={self.answer_sentence[:80]!r} "
            f"-> {self.verdict}"
        )


def _coerce_str_list(v) -> list[str]:
    if not isinstance(v, list):
        return []
    return [str(x).strip() for x in v if str(x).strip()]


def _parse(result, flagged: list[str]) -> Optional[list[ClassifiedClaim]]:
    """One ClassifiedClaim per flagged claim, in order, or None if the model's
    output does not cover every claim with a known kind."""
    if not isinstance(result, dict):
        return None
    items = result.get("claims")
    if not isinstance(items, list) or len(items) != len(flagged):
        return None
    out: list[ClassifiedClaim] = []
    for src, item in zip(flagged, items):
        if not isinstance(item, dict):
            return None
        kind = str(item.get("kind") or "").strip().lower()
        if kind not in _KINDS:
            return None
        scope = item.get("scope")
        out.append(ClassifiedClaim(
            claim=src,
            kind=kind,
            scope=str(scope).strip().lower() if scope else None,
            concept=str(item.get("concept") or "").strip(),
            field_keywords=_coerce_str_list(item.get("field_keywords")),
            matching_fields=_coerce_str_list(item.get("matching_fields")),
            reason=str(item.get("reason") or "").strip(),
            answer_sentence=str(item.get("answer_sentence") or "").strip(),
        ))
    return out


async def classify_flagged_claims(
    answer: str,
    flagged: list[str],
    inventory: RecordInventory,
) -> Optional[list[ClassifiedClaim]]:
    """Classify each judge-flagged claim. None on any failure (fail-closed)."""
    flagged = [c for c in (flagged or []) if c and c.strip()]
    if not flagged:
        return None

    numbered = "\n".join(f"{i}. {c}" for i, c in enumerate(flagged, 1))
    user_input = (
        f"RECORD INVENTORY (family: fields):\n{inventory.render()}\n\n"
        f"FLAGGED CLAIMS (classify each, in order):\n{numbered}\n\n"
        f"ANSWER (context only — the flagged claims are the unit):\n"
        f"{(answer or '')[:_ANSWER_CONTEXT_CHARS]}"
    )
    try:
        result, raw = await call_llm_json(
            system_prompt=_SYSTEM_PROMPT,
            user_message=user_input,
            temperature=0.0,
            # Same budget reasoning as verifier.py: the local model's
            # thinking trace precedes the JSON; the cloud path needs less.
            max_tokens=2000,
            cloud_max_tokens=900,
            role="reasoning",
            validate=lambda r: isinstance(r, dict) and isinstance(r.get("claims"), list),
            schema_hint='"claims" (array of objects, one per flagged claim, each with "claim", "kind", "answer_sentence", "scope", "concept", "field_keywords", "matching_fields", "reason")',
            _call_llm=call_llm,
        )
    except Exception as exc:  # noqa: BLE001 — fail-closed by design
        logger.error("Schema-absence classifier failed (%s) — rejection stands.", exc)
        return None
    parsed = _parse(result, flagged)
    if parsed is None:
        logger.error(
            "Schema-absence classifier returned no usable JSON (raw: %s) — rejection stands.",
            (raw or "")[:200],
        )
    return parsed


def ground_against_inventory(
    claims: list[ClassifiedClaim], inventory: RecordInventory, answer: str = ""
) -> list[ClassifiedClaim]:
    """Fill `keyword_hits`, `confirmed_absent` and `verdict` on each claim.

    A claim is `confirmed_absent` only when ALL of:
      * the classifier called it a schema absence;
      * it passes `sentence_guard()` — the judge's claim (wrapper stripped)
        and the answer sentence it quotes are BOTH negative and BOTH speak
        of a field / column / what the records track or capture, and the
        quote is genuinely in the answer;
      * the classifier, shown the inventory, named NO existing field for it;
      * the keyword scan over the claim's scope finds NO field or family
        (a None scan — unknown scope or no usable keyword — is a failure
        to confirm, not a confirmation).
    """
    for c in claims:
        if c.kind != KIND_SCHEMA_ABSENCE:
            c.verdict = f"not_schema:{c.kind}"
            c.confirmed_absent = False
            continue
        guard = sentence_guard(c.claim, c.answer_sentence, answer)
        if guard is not None:
            c.verdict = f"unconfirmable:{guard}"
            c.confirmed_absent = False
            continue
        c.keyword_hits = fields_serving(c.field_keywords, c.scope, inventory)
        if c.matching_fields:
            c.verdict = "refuted:classifier_named_existing_field"
        elif c.keyword_hits is None:
            c.verdict = "unconfirmable:unknown_scope_or_no_keyword"
        elif c.keyword_hits:
            c.verdict = "refuted:field_exists"
        else:
            c.verdict = "confirmed_absent"
            c.confirmed_absent = True
    return claims
