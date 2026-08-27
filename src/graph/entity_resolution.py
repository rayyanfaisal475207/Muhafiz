# ============================================================
# Entity resolution — CNIC-first, name-fallback (Phase 4.8)
#
# Implements architecture doc §7.3's three-tier design:
#
#   cnic_auto        — exact CNIC match. Auto-merge: the mention attaches
#                       directly to the existing canonical node, no new
#                       node, no SAME_AS edge (there was never a second
#                       entity to link — see docs/graph_schema.md).
#   flagged_unverified — no CNIC (or CNIC absent on the candidate), strong
#                       name similarity, usually with corroborating
#                       context. Surfaced to investigators with its basis
#                       shown; never auto-merged.
#   human_review      — weak name-only match, no CNIC, no corroboration.
#                       Held in the review queue, not surfaced as probable.
#
# Hard rule, structural not scored: a mention with a CNIC is NEVER even
# compared by name against a candidate with a DIFFERENT non-empty CNIC —
# excluded at blocking, before any score is computed. Different CNICs
# never merge "regardless of name similarity" is enforced by construction,
# not by a threshold that could be tuned away.
#
# CNIC-tier decisions never reach the LLM adjudicator — they don't need a
# judgment call. Only genuinely medium-confidence name-fallback pairs do.
# ============================================================

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from rapidfuzz import fuzz

from src.extraction.llm_json import parse_json_response
from src.graph import age_client, identity_index, ingestion_quality, versioning
from src.graph.case_scope import scoped_cypher
from src.ingestion.script_detector import _ARABIC_SCRIPT
from src.ingestion.text_normalizer import normalize_urdu
from src.llm.client import call_llm

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "resolution_adjudicator.txt"
_SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")

# Captured once at import time — NOT re-read as `age_client.GRAPH_NAME` at
# call time below, because tests (and any other caller) monkeypatch the
# whole `age_client` module-level reference with a fake that has no
# GRAPH_NAME attribute. This is the one production graph name the
# Postgres-side identity_index table is ever consulted for (Milestone A1)
# — the isolated eval graph's resolution decisions must never read or be
# influenced by the shared production index.
_PRODUCTION_GRAPH = age_client.GRAPH_NAME

TYPE_TO_LABEL = {
    "person": "Person",
    "vehicle": "Vehicle",
    "phone": "PhoneNumber",
    "address": "Address",
    "location": "Address",
    "organization": "Organization",
    "incident": "Incident",
    # Milestone B2 (GRAPH_SCALE_SCHEMA_EXPANSION_PLAN.md — officer identity
    # resolution): Officer gets the exact same tier discipline CNIC-based
    # Person resolution already gets, keyed on belt_no instead of cnic —
    # see TYPE_PRIMARY_ID_KEY below.
    "officer": "Officer",
}

# The exact-match identifier that gates the auto-merge tier, per entity
# type. CNIC is the person case the architecture doc names explicitly,
# but the identical hard-block safety property is required for vehicles
# too — the near-miss-plate test cases (V-NM1A/ICT-FL-273 vs
# V-NM1B/ICT-FL-274, one digit apart) are exactly the plate analogue of
# "different CNICs never merge, regardless of name similarity." Types with
# no entry here (organization, address) have no reliable exact-match
# identifier and go through name-fallback only.
#
# "phone" added 2026-08-04 alongside wiring PhoneNumber mentions into the
# graph for the first time (see ingestion/service.py's
# _write_phone_mentions) — without this, two mentions of the identical
# phone number would fall through to name-similarity scoring on
# canonical_name (which IS the phone number here, so it would likely still
# score near-exact and merge via TIER_FLAGGED) instead of the stronger,
# immediate TIER_CNIC_AUTO an exact structured identifier deserves — same
# reasoning TYPE_PRIMARY_ID_KEY already applies to cnic/plate.
TYPE_PRIMARY_ID_KEY = {
    "person": "cnic",
    "vehicle": "plate",
    "phone": "phone",
    # "officer"/"belt_no" added Milestone B2 — an officer's belt number is
    # exactly the same kind of exact-match structured identifier CNIC/
    # plate/phone already are: two mentions with the SAME belt_no are the
    # same officer (cnic_auto-equivalent auto-merge, no name comparison
    # needed), and a mention with a belt_no is never even compared by name
    # against a candidate with a DIFFERENT non-empty belt_no (the same
    # hard-block invariant _generate_candidates() already enforces for
    # every other key in this dict).
    "officer": "belt_no",
}

# Named after its original (and still primary) case — CNIC — but the
# mechanism is generic: exact match on TYPE_PRIMARY_ID_KEY[entity_type].
# Stored in the graph as the literal tier value for every entity type that
# resolves through it, vehicles included; kept as one name rather than a
# CNIC-only and a plate-only tier so 4.11's review queue/eval code has one
# "was this an exact-identifier match" tier to check, not N of them.
TIER_CNIC_AUTO = "cnic_auto"
TIER_FLAGGED = "flagged_unverified"
TIER_REVIEW = "human_review"
TIER_NEW = "new"

# Name-fallback score is ALWAYS capped below the CNIC tier's implicit 1.0,
# no matter how strong its components — architecture §7.3's explicit
# requirement that name-fallback confidence never reaches auto-merge.
NAME_FALLBACK_CAP = 0.85

# Tier thresholds on name similarity (0-1, rapidfuzz token_sort_ratio/100).
# Starting points — tuned against data/memory/entity_roster.csv's labeled
# ground truth by scripts/eval_entity_resolution.py (4.11), not hand-picked
# once and frozen.
NEAR_EXACT_NAME = 0.90        # near-identical name alone is enough to flag
                               # (the P-006 cross-case repeat-offender case:
                               # no shared case/structured-id by definition)
HIGH_NAME_WITH_CONTEXT = 0.75  # + corroboration (shared case/structured id)
MEDIUM_BAND_FLOOR = 0.55       # below this and above REVIEW_FLOOR: ask the LLM
REVIEW_FLOOR = 0.40            # below this: too weak to surface at all

_STRUCTURED_ID_KEYS = ("phone", "plate", "father_name")


@dataclass
class Candidate:
    entity_id: str
    node: dict
    name_similarity: float
    shared_case: bool
    shared_structured_id: bool
    score: float


@dataclass
class ResolutionDecision:
    tier: str                          # cnic_auto | flagged_unverified | human_review | new
    target_entity_id: Optional[str]    # the existing node this mention resolves toward, if any
    confidence: float
    basis: str                         # human-readable — what the review queue shows investigators
    candidates_considered: int = 0
    # Milestone D1 (GRAPH_SCALE_SCHEMA_EXPANSION_PLAN.md — pending-candidate
    # reprioritization): the raw Candidate-scoring fields behind `basis`,
    # carried structurally rather than left to be re-parsed out of the
    # free-text basis string. None for cnic_auto/new (no Candidate — see
    # resolve_mention: these tiers return before a Candidate is ever
    # scored). Persisted onto the SAME_AS edge itself by resolve_and_write
    # below, so candidate_reprioritization.py's re-scoring job can diff
    # "the signal at original-match time" against a freshly recomputed
    # Candidate without an LLM call — a deterministic structural diff.
    name_similarity: Optional[float] = None
    shared_case: Optional[bool] = None
    shared_structured_id: Optional[bool] = None


def _new_entity_id(entity_type: str) -> str:
    return f"{entity_type.upper()}-{uuid.uuid4().hex[:10]}"


# Roman-Urdu <-> Urdu-script name bridging (A-2). Ordinary token_sort_ratio
# compares character sequences, so a genuinely same name in different
# scripts ("Zafar Iqbal" / "ظفر اقبال") scores ~0 — the two strings share
# no characters at all, regardless of how similar they sound. Rather than a
# transliteration library (no Urdu-specific one is installed, and pulling
# one in for this is a heavier dependency than the corpus currently
# justifies) or an LLM call on every cross-script pair (adds latency/cost
# and a new failure mode to a merge-decision path), this maps both names
# down to a coarse CONSONANT SKELETON — Urdu consonant letters mapped to
# their Roman equivalent, vowel/glide letters (ا و ی ے ع ء ھ) dropped on
# the Urdu side, and plain vowels (a e i o u) dropped on the Roman side —
# then fuzzy-compares the two skeletons. "Zafar"->"zfr", "ظفر"->"zfr":
# exact match. Approximate by design (a heuristic starting point, same as
# entity_roster.csv-tuned thresholds elsewhere in this file), not a
# phonetically rigorous transliteration.
_URDU_CONSONANT_MAP = {
    "ب": "b", "پ": "p", "ت": "t", "ٹ": "t", "ث": "s", "ج": "j", "چ": "ch",
    "ح": "h", "خ": "kh", "د": "d", "ڈ": "d", "ذ": "z", "ر": "r", "ڑ": "r",
    "ز": "z", "ژ": "zh", "س": "s", "ش": "sh", "ص": "s", "ض": "z", "ط": "t",
    "ظ": "z", "غ": "gh", "ف": "f", "ق": "q", "ک": "k", "گ": "g", "ل": "l",
    "م": "m", "ن": "n", "ں": "n", "ہ": "h",
    # Deliberately NOT mapped (vowel/glide carriers, dropped from the
    # skeleton entirely): ا ع ء ھ و ی ے
}
_ROMAN_VOWELS_RE = re.compile(r"[aeiou]")
_NON_LATIN_LETTER_RE = re.compile(r"[^a-z]")


def _consonant_skeleton(text: str) -> str:
    """Coarse cross-script phonetic skeleton — see comment above."""
    norm = normalize_urdu(text)
    if _ARABIC_SCRIPT.search(norm):
        return "".join(_URDU_CONSONANT_MAP.get(ch, "") for ch in norm)
    return _ROMAN_VOWELS_RE.sub("", _NON_LATIN_LETTER_RE.sub("", norm.lower()))


def _name_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    base = fuzz.token_sort_ratio(normalize_urdu(a), normalize_urdu(b)) / 100.0

    # Only attempt the cross-script skeleton comparison when the two names
    # are actually in DIFFERENT scripts — ordinary same-script comparisons
    # already work via token_sort_ratio above and must be completely
    # unaffected by this addition (a wrong merge isn't free to undo, per
    # versioning.py's append-only edge model). This can only ever raise the
    # score for a pair that scored ~0 above, never lower an existing
    # same-script match.
    a_is_arabic = bool(_ARABIC_SCRIPT.search(a))
    b_is_arabic = bool(_ARABIC_SCRIPT.search(b))
    if a_is_arabic != b_is_arabic:
        skel_a, skel_b = _consonant_skeleton(a), _consonant_skeleton(b)
        if skel_a and skel_b:
            skeleton_score = fuzz.ratio(skel_a, skel_b) / 100.0
            base = max(base, skeleton_score)

    return base


def _has_shared_structured_id(mention: dict, candidate_props: dict) -> bool:
    for key in _STRUCTURED_ID_KEYS:
        m_val, c_val = mention.get(key), candidate_props.get(key)
        if m_val and c_val and str(m_val).strip() == str(c_val).strip():
            return True
    return False


# ── Graph reads (blocking/candidate generation) ────────────────────────

async def _find_by_primary_id(
    label: str, id_key: str, id_value: str, *, graph: str = age_client.GRAPH_NAME
) -> Optional[dict]:
    """
    Graph Scale & Schema Expansion, Milestone A1: reads the Postgres
    identity index first (O(1) primary-key lookup — see
    src/graph/identity_index.py) rather than going straight to the AGE
    scan below. The Cypher MATCH on an exact property equality below looks
    targeted but AGE has no property index behind it, so it was actually a
    full label scan — see migrations/021_identity_index.sql's header for
    the confirmed diagnosis.

    An index HIT returns immediately without touching AGE at all — the
    only thing the one caller (resolve_mention) reads off the result is
    `entity_id`, so a synthetic node dict carrying just that is a complete
    substitute.

    An index MISS (never indexed, or the index write failed/hasn't
    happened yet) falls through to the exact same AGE scan this function
    always ran — the plan's explicit "defends against drift between the
    index and the graph" requirement. `graph="evidence_graph_eval"` (the
    isolated eval graph) is deliberately never looked up in the shared
    production identity_index table — see the `graph` parameter guard
    below.
    """
    if graph == _PRODUCTION_GRAPH:
        indexed_entity_id = await identity_index.lookup(label, id_key, id_value)
        if indexed_entity_id:
            return {"properties": {"entity_id": indexed_entity_id}}

    rows = await age_client.execute_cypher(
        f"MATCH (n:{label} {{{id_key}: $id_value}}) RETURN n",
        params={"id_value": id_value},
        columns=["n"],
        graph=graph,
    )
    return rows[0]["n"] if rows else None


async def _fetch_all_nodes(label: str, *, graph: str = age_client.GRAPH_NAME) -> list[dict]:
    """
    Full scan of every existing node of this type. Cross-case by design —
    not case_id-filtered — because cross-case resolution (the P-006
    repeat-offender flagship case) requires comparing against canonical
    entities regardless of which case they were first seen in.

    Still the exact fallback used on an identity-index miss (Milestone
    A1) — kept as its own function rather than folded into
    _fetch_nodes_excluding() below, since a mention with no id_key at all
    (organization/address) or no id_value on this particular mention has
    no exclude set to build and must scan everything, same as before A1.
    """
    rows = await age_client.execute_cypher(f"MATCH (n:{label}) RETURN n", columns=["n"], graph=graph)
    return [r["n"] for r in rows]


async def _fetch_nodes_excluding(
    label: str, excluded_entity_ids: list[str], *, graph: str = age_client.GRAPH_NAME
) -> list[dict]:
    """
    Same full scan as _fetch_all_nodes(), minus every entity_id already
    known (via the Postgres identity index) to carry a DIFFERENT non-empty
    primary-id value than the mention being resolved — see
    identity_index.entity_ids_excluding()'s docstring for why excluding
    them here is safe (they would be discarded by _generate_candidates()'s
    hard block regardless).

    Still an AGE label scan under the hood (AGE has no property index to
    push `NOT IN` down to) — this shrinks what gets pulled out of it and
    iterated in Python, it does not make the underlying scan itself O(1).
    The genuinely O(1) win is _find_by_primary_id()'s index hit, which
    means this function is reached far less often in the first place: only
    when a mention's id_value doesn't match anything yet indexed.
    """
    rows = await age_client.execute_cypher(
        f"MATCH (n:{label}) WHERE NOT n.entity_id IN $excluded RETURN n",
        params={"excluded": excluded_entity_ids},
        columns=["n"],
        graph=graph,
    )
    return [r["n"] for r in rows]


async def _shares_case_batch(
    entity_ids: list[str], case_id: str, *, graph: str = age_client.GRAPH_NAME
) -> set[str]:
    """
    Module 7.3: which of `entity_ids` share `case_id`, in one round trip.

    Replaces what used to be one _shares_case() Cypher call per surviving
    candidate in _generate_candidates()'s loop (an N+1 during ingestion —
    the audit's own note is this is bounded by "dozens" of candidates per
    mention, not thousands, but still N round trips where one suffices).
    Same result set as calling the old per-entity check for each id —
    this is a query-shape change, not a resolution-logic change.
    """
    if not entity_ids:
        return set()
    rows = await scoped_cypher(
        "MATCH (n)-[:BELONGS_TO_CASE]->(c:Case {case_id: $case_id}) "
        "WHERE n.entity_id IN $entity_ids "
        "RETURN n.entity_id AS entity_id",
        case_id,
        params={"entity_ids": entity_ids},
        columns=["entity_id"],
        graph=graph,
    )
    return {row["entity_id"] for row in rows}


async def _case_hoisted_id_value(
    label: str, id_key: str, mention_name: str, case_id: str, *, graph: str = age_client.GRAPH_NAME
) -> Optional[str]:
    """
    Case-scoped analogue of src/ingestion/service.py's
    _document_cnic_for_name() (the "if his CNIC is anywhere in this
    document, it belongs to him" fix) — one level up, across documents.

    A mention arriving with NO primary-id value of its own (the common
    case for a roznamcha/zimni entry that just names someone by their
    given name, with no ID-card line in that specific document) still
    deserves a shot at TIER_CNIC_AUTO if some OTHER document already in
    this SAME case recorded a primary id for a name that safely contains
    this one — e.g. an earlier FIR narrative's "فیصل ولد محمد رمضان،
    شناختی کارڈ ..." line, with this mention just "فیصل".

    Same token-containment-with-refusal rule as the document-scoped
    version: a bare given name matches a longer name whose tokens start
    with the same tokens, and if two DIFFERENT full names already in
    this case both contain the short name with DIFFERENT id values, this
    refuses to guess — returns None rather than picking one, never a
    silent wrong merge.

    Deliberately scoped to case_id (via BELONGS_TO_CASE, through
    case_scope.scoped_cypher's enforcement chokepoint), unlike
    _generate_candidates()'s own deliberately cross-case scan (the P-006
    repeat-offender design). Hoisting an id value onto a bare name routes
    straight to TIER_CNIC_AUTO — an immediate merge, no SAME_AS, no
    review — which is a much stronger, effectively irreversible action
    than merely flagging a candidate for a human. That's only safe to
    trust by default inside the one boundary (a single case) where "same
    given name, no other evidence it's a different person" is the
    ordinary, expected case — not across cases, where two unrelated
    people sharing a first name is unremarkable.
    """
    rows = await scoped_cypher(
        f"MATCH (n:{label})-[b:BELONGS_TO_CASE]->(c:Case {{case_id: $case_id}}) "
        f"WHERE b.superseded_by IS NULL AND n.{id_key} IS NOT NULL AND n.{id_key} <> '' "
        f"RETURN DISTINCT n.canonical_name AS name, n.{id_key} AS id_value",
        case_id, params=None, columns=["name", "id_value"], graph=graph,
    )
    name_tokens = normalize_urdu(mention_name).split()
    if not name_tokens:
        return None

    found: set[str] = set()
    for row in rows:
        other_name = row.get("name") or ""
        other_id = row.get("id_value")
        if not other_id or other_name == mention_name:
            continue
        other_tokens = normalize_urdu(other_name).split()
        if not other_tokens:
            continue
        shorter, longer = (
            (name_tokens, other_tokens) if len(name_tokens) <= len(other_tokens)
            else (other_tokens, name_tokens)
        )
        if longer[: len(shorter)] == shorter:
            found.add(other_id)

    return next(iter(found)) if len(found) == 1 else None


async def _generate_candidates(
    label: str, mention: dict, case_id: str, id_key: Optional[str] = None,
    *, graph: str = age_client.GRAPH_NAME,
) -> list[Candidate]:
    mention_id = mention.get(id_key) if id_key else None
    mention_name = mention.get("canonical_name", "")

    # Milestone A1: when this mention carries an id_value, the Postgres
    # identity index already tells us every entity_id that has a DIFFERENT
    # value recorded for this id_key — those are excluded below by the
    # hard-block rule regardless, so there's no need to pull their full
    # node properties out of AGE just to discard them. Only consulted for
    # the production graph, same "never look at production's index for an
    # eval-graph run" guard as _find_by_primary_id().
    excluded_entity_ids: list[str] = []
    if mention_id and graph == _PRODUCTION_GRAPH:
        excluded_entity_ids = await identity_index.entity_ids_excluding(label, id_key, mention_id)

    if excluded_entity_ids:
        all_nodes = await _fetch_nodes_excluding(label, excluded_entity_ids, graph=graph)
    else:
        all_nodes = await _fetch_all_nodes(label, graph=graph)

    # Pass 1: apply the hard block + name-similarity floor, same as
    # before — just deferring the shared_case lookup so every surviving
    # candidate's check can be batched into one call below.
    surviving: list[tuple[dict, str, float]] = []
    for node in all_nodes:
        props = node.get("properties", {})
        node_id = props.get(id_key) if id_key else None

        # Hard block — never scored, never surfaced, regardless of name
        # similarity. This is the P-DRY-001/002, P-CP2A/B (CNIC) and
        # V-NM1A/B, V-NM2A/B (plate) invariant.
        if mention_id and node_id and str(node_id) != str(mention_id):
            continue

        name_sim = _name_similarity(mention_name, props.get("canonical_name", ""))
        if name_sim < REVIEW_FLOOR:
            continue

        entity_id = props.get("entity_id")
        if not entity_id:
            continue

        surviving.append((node, entity_id, name_sim))

    shared_case_ids = await _shares_case_batch(
        [entity_id for _, entity_id, _ in surviving], case_id, graph=graph
    )

    candidates: list[Candidate] = []
    for node, entity_id, name_sim in surviving:
        props = node.get("properties", {})
        shared_case = entity_id in shared_case_ids
        shared_structured = _has_shared_structured_id(mention, props)

        score = min(
            name_sim * 0.7
            + (0.15 if shared_structured else 0.0)
            + (0.15 if shared_case else 0.0),
            NAME_FALLBACK_CAP,
        )
        candidates.append(Candidate(
            entity_id=entity_id, node=node, name_similarity=name_sim,
            shared_case=shared_case, shared_structured_id=shared_structured,
            score=score,
        ))

    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates


# ── LLM adjudication (medium-confidence name-fallback pairs only) ──────

async def _adjudicate(mention: dict, candidate: Candidate, case_id: str) -> Optional[dict]:
    user_message = (
        f"NEW mention: name={mention.get('canonical_name')!r}, "
        f"father_name={mention.get('father_name')!r}, "
        f"address={mention.get('address')!r}, case={case_id!r}\n"
        f"EXISTING candidate: name={candidate.node['properties'].get('canonical_name')!r}, "
        f"father_name={candidate.node['properties'].get('father_name')!r}, "
        f"address={candidate.node['properties'].get('address')!r}, "
        f"name_similarity={candidate.name_similarity:.2f}, "
        f"shared_case={candidate.shared_case}, "
        f"shared_structured_id={candidate.shared_structured_id}"
    )
    try:
        response = await call_llm(
            system_prompt=_SYSTEM_PROMPT, user_message=user_message,
            # 800 wasn't enough on live re-measurement — Qwen3-14B's
            # thinking trace can exhaust it before the answer. Raised to
            # 2000 for the LOCAL budget; cloud_max_tokens pinned at the
            # old 800 so the cloud fallback is unaffected.
            temperature=0.0, max_tokens=2000, cloud_max_tokens=800,
        )
    except Exception as exc:
        logger.warning("Resolution adjudicator LLM call failed: %s", exc)
        return None
    parsed = parse_json_response(response, context="resolution_adjudicator")
    if not isinstance(parsed, dict):
        return None
    return parsed


# ── Core resolution decision ────────────────────────────────────────────

async def resolve_mention(
    entity_type: str, mention: dict, case_id: str, *, graph: str = age_client.GRAPH_NAME
) -> ResolutionDecision:
    """
    Decide how a newly-extracted mention resolves against existing
    canonical entities of `entity_type` ("person" | "vehicle" | "phone" |
    "address" | "organization"). Does not write anything — see
    resolve_and_write() for the write-through-versioning.py step.

    `graph` defaults to the production graph. Every read this function
    triggers (primary-id lookup, full-label scan, case-membership check)
    is scoped to it — this must match whatever `graph` resolve_and_write()
    is about to write to, or resolution decisions would be computed
    against a different graph's data than the one being written (Phase 3,
    Module 3.1: this is what keeps eval runs from silently scoring
    candidates against real production entities).
    """
    label = TYPE_TO_LABEL.get(entity_type)
    if label is None:
        raise ValueError(f"Unknown entity_type for resolution: {entity_type!r}")

    id_key = TYPE_PRIMARY_ID_KEY.get(entity_type)
    id_value = mention.get(id_key) if id_key else None
    if id_value:
        existing = await _find_by_primary_id(label, id_key, id_value, graph=graph)
        if existing:
            target_id = existing["properties"].get("entity_id")
            return ResolutionDecision(
                tier=TIER_CNIC_AUTO, target_entity_id=target_id, confidence=1.0,
                basis=f"matched on {id_key} ({id_value})", candidates_considered=1,
            )
    elif id_key and case_id:
        # [case-wide hoisting] This mention carries no primary id of its
        # own — before falling to name-fallback scoring, check whether
        # some OTHER document already in this SAME case recorded one for
        # a name that safely contains this one. See
        # _case_hoisted_id_value()'s own docstring for the containment-
        # with-refusal rule and why this stays case-scoped, unlike
        # _generate_candidates()'s deliberately cross-case scan below.
        hoisted_id = await _case_hoisted_id_value(
            label, id_key, mention.get("canonical_name", ""), case_id, graph=graph,
        )
        if hoisted_id:
            existing = await _find_by_primary_id(label, id_key, hoisted_id, graph=graph)
            if existing:
                target_id = existing["properties"].get("entity_id")
                return ResolutionDecision(
                    tier=TIER_CNIC_AUTO, target_entity_id=target_id, confidence=1.0,
                    basis=f"matched on {id_key} ({hoisted_id}) hoisted from elsewhere in case {case_id}",
                    candidates_considered=1,
                )

    candidates = await _generate_candidates(label, mention, case_id, id_key=id_key, graph=graph)
    if not candidates:
        return ResolutionDecision(tier=TIER_NEW, target_entity_id=None, confidence=0.0,
                                   basis="no matching candidate found", candidates_considered=0)

    best = candidates[0]

    if best.name_similarity >= NEAR_EXACT_NAME:
        basis = "matched on near-identical name"
        if best.shared_structured_id:
            basis += " + shared structured identifier"
        if best.shared_case:
            basis += " + shared case"
        return ResolutionDecision(
            TIER_FLAGGED, best.entity_id, best.score, basis, len(candidates),
            name_similarity=best.name_similarity, shared_case=best.shared_case,
            shared_structured_id=best.shared_structured_id,
        )

    if best.name_similarity >= HIGH_NAME_WITH_CONTEXT and (best.shared_case or best.shared_structured_id):
        basis = "matched on name + " + (
            "shared structured identifier" if best.shared_structured_id else "shared case"
        ) + ", unverified"
        return ResolutionDecision(
            TIER_FLAGGED, best.entity_id, best.score, basis, len(candidates),
            name_similarity=best.name_similarity, shared_case=best.shared_case,
            shared_structured_id=best.shared_structured_id,
        )

    if best.name_similarity >= MEDIUM_BAND_FLOOR:
        verdict = await _adjudicate(mention, best, case_id)
        if verdict and verdict.get("same_entity"):
            tier = verdict.get("tier")
            if tier not in (TIER_FLAGGED, TIER_REVIEW):
                # Defensive: the adjudicator is never allowed to grant
                # auto-merge — CNIC is the only path there. An
                # out-of-vocabulary/malformed tier degrades to the safer
                # (lower-visibility) review tier, not silently to flagged.
                tier = TIER_REVIEW
            confidence = min(float(verdict.get("confidence", best.score) or best.score), NAME_FALLBACK_CAP)
            basis = f"matched on name + LLM adjudication: {verdict.get('reasoning', '')}".strip()
            return ResolutionDecision(
                tier, best.entity_id, confidence, basis, len(candidates),
                name_similarity=best.name_similarity, shared_case=best.shared_case,
                shared_structured_id=best.shared_structured_id,
            )
        # LLM said not the same entity, or adjudication failed — fall
        # through to the weak-match/no-candidate bands below rather than
        # silently promoting a medium-confidence name-only match.

    if best.name_similarity >= REVIEW_FLOOR:
        return ResolutionDecision(
            TIER_REVIEW, best.entity_id, best.score,
            "weak name-only match, no CNIC, no corroborating shared attributes",
            len(candidates),
            name_similarity=best.name_similarity, shared_case=best.shared_case,
            shared_structured_id=best.shared_structured_id,
        )

    return ResolutionDecision(TIER_NEW, None, 0.0, "no matching candidate found", len(candidates))


# ── Write-through (resolution decision -> versioned graph write) ──────

async def resolve_and_write(
    entity_type: str,
    mention: dict,
    case_id: str,
    source_doc_id: str,
    source_chunk_id: Optional[str] = None,
    *,
    graph: str = age_client.GRAPH_NAME,
) -> dict:
    """
    Resolve `mention` and write the result through versioning.py.

    Returns {"entity_id", "tier", "confidence", "basis", "is_new_node"} —
    the entity_id is always the node this mention's evidence attaches to
    (the existing canonical node for cnic_auto, otherwise a freshly minted
    node for the mention itself — see module docstring for why
    name-fallback tiers never physically merge into the candidate node).

    `graph` defaults to the production graph — production ingestion never
    overrides it, so this is a no-op for real behavior. It is threaded
    into BOTH the resolve_mention() read path and every versioning.py
    write below, so a caller targeting a non-default graph (currently only
    scripts/eval_entity_resolution.py, passing "evidence_graph_eval") gets
    fully isolated reads and writes, not writes-only isolation (Phase 3,
    Module 3.1).
    """
    label = TYPE_TO_LABEL[entity_type]
    decision = await resolve_mention(entity_type, mention, case_id, graph=graph)

    if decision.tier == TIER_CNIC_AUTO:
        entity_id = decision.target_entity_id
        is_new_node = False
    else:
        entity_id = _new_entity_id(entity_type)
        is_new_node = True

    node_properties = {k: v for k, v in mention.items() if v is not None}
    await versioning.write_node(
        label, {"entity_id": entity_id}, node_properties,
        source_doc_id=source_doc_id, confidence=decision.confidence if decision.tier != TIER_NEW else 1.0,
        graph=graph,
    )

    await versioning.write_edge(
        "BELONGS_TO_CASE", label, {"entity_id": entity_id}, "Case", {"case_id": case_id},
        {}, source_doc_id=source_doc_id, source_chunk_id=source_chunk_id, confidence=1.0,
        graph=graph,
    )
    await versioning.write_edge(
        "APPEARS_IN", label, {"entity_id": entity_id}, "Document", {"doc_id": source_doc_id},
        {"surface_text": mention.get("canonical_name", "")},
        source_doc_id=source_doc_id, source_chunk_id=source_chunk_id,
        confidence=mention.get("extraction_confidence", 1.0),
        graph=graph,
    )

    if entity_type == "incident" and mention.get("date"):
        date_str = mention["date"]
        await versioning.write_node(
            "Date", {"date": date_str}, {},
            source_doc_id=source_doc_id, confidence=1.0,
            graph=graph,
        )
        await versioning.write_edge(
            "OCCURRED_ON", label, {"entity_id": entity_id}, "Date", {"date": date_str},
            {}, source_doc_id=source_doc_id, source_chunk_id=source_chunk_id, confidence=1.0,
            graph=graph,
        )

    if decision.tier in (TIER_FLAGGED, TIER_REVIEW) and decision.target_entity_id:
        await versioning.write_edge(
            "SAME_AS", label, {"entity_id": entity_id}, label, {"entity_id": decision.target_entity_id},
            {
                "tier": decision.tier,
                "basis": decision.basis,
                "status": "pending",
                # Milestone D1: the Candidate-scoring fields behind
                # `basis`, persisted structurally (see ResolutionDecision's
                # own docstring) so candidate_reprioritization.py never
                # has to re-parse them out of free text or call an LLM.
                "name_similarity": decision.name_similarity,
                "shared_case": decision.shared_case,
                "shared_structured_id": decision.shared_structured_id,
            },
            source_doc_id=source_doc_id, source_chunk_id=source_chunk_id,
            confidence=decision.confidence,
            graph=graph,
        )

    # [Ingestion Quality Control at Scale, Module G1] The one chokepoint
    # every person/vehicle/phone/officer resolution in this codebase
    # passes through with a final tier decision already made — pure
    # visibility, no new judgment call. Production-graph only, same
    # `_PRODUCTION_GRAPH` guard A1's identity_index read path already
    # uses, so an eval run against evidence_graph_eval never pollutes a
    # real ingestion run's quality counts.
    if graph == _PRODUCTION_GRAPH:
        ingestion_quality.record_tier(decision.tier)

    return {
        "entity_id": entity_id,
        "tier": decision.tier,
        "confidence": decision.confidence,
        "basis": decision.basis,
        "is_new_node": is_new_node,
        "candidates_considered": decision.candidates_considered,
    }
