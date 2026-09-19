# Verification Policy Design

**Design review only. No code was modified.**

Given the three approved decisions — full algebra exposed, selective
verification, deterministic rules authoritative over LLM recommendation —
this proposes the deterministic policy that decides *when* all three routes
must run.

---

## 0. The structural constraint that shapes everything

Verification must be decided **before execution**, because running AGE is
the expensive act being gated (median 41 s, max 92 s). That splits the
available signals in two, and the split is not negotiable:

| Available **before** execution | Available **only after** |
|---|---|
| `QueryRequirements` (8 fields, derived from the typed spec) | `CoverageVerdict` — `build_coverage()` needs `observed_n` |
| `SelectionOutcome` (profile, escalated, corrected) | `GateEvaluation` — reads a receipt |
| Registry facts: property presence, fanout, versioning, tombstones, `distinct_key` | `ReconciliationResult` |
| `ValidationResult` (spec validates cleanly?) | actual row counts |

**Consequence:** `CoverageVerdict` and gate outcomes — the two signals most
tempting to use as triggers — **cannot be inputs to the pre-execution
decision.** A policy keyed on them would be deciding after the cost was
already paid.

The registry *does* know presence and fanout ahead of time, so the
*predictable* form of those risks is usable. That is the substitution this
design rests on.

---

## 1. Mandatory verification triggers

All fire from the typed spec plus the registry. Each overrides any LLM
recommendation.

### T1 · Composite / multi-source execution

| | |
|---|---|
| **Condition** | `QueryRequirements.requires_multiple_sources` |
| **Source** | `gates.requirements_from_spec()` → `authority.resolve_authority()` |
| **Reason** | The answer is assembled from two authorities joined on stable ids. No single route saw the whole computation. |
| **Risk prevented** | The measured Phase 6 failure: a date restriction dropped, 73 served where truth was 51. |
| **Corpus** | **12 of 42** |

### T2 · Relationship traversal present

| | |
|---|---|
| **Condition** | `traversal_count > 0` |
| **Source** | `QueryRequirements.traversal_count` |
| **Reason** | Traversal is where grain becomes ambiguous — the 4-vs-70 and 92-vs-94 families both live here. |
| **Risk prevented** | Counting edges as entities; role filters silently dropped. |
| **Corpus** | **9 of 42** |

### T3 · Fanning traversal *(narrower, stronger form of T2)*

| | |
|---|---|
| **Condition** | any traversal whose `RelationshipInfo.fans_in_direction(travel)` is true |
| **Source** | registry, pre-execution |
| **Reason** | Row multiplication is the single most-measured defect class (73→449, 73→2100). |
| **Risk prevented** | Inflated counts that look plausible. |
| **Note** | Subsumed by T2 today but stated separately: if T2 is ever relaxed, this must survive. |

### T4 · Sparse field in the measure or a filter

| | |
|---|---|
| **Condition** | `value_field` or any `FieldPredicate.field` whose registry presence < 50% |
| **Source** | `EntityInfo.property_presence()` — known pre-execution |
| **Reason** | The *predictable* form of INSUFFICIENT coverage. `Person.age` is 19/430; an average over it describes 4% of the population. |
| **Risk prevented** | A confident figure over a fraction of the population. |
| **Corpus** | **6 of 42** |
| **Why not `CoverageVerdict`** | That verdict is post-execution. Presence is the pre-execution proxy and is derived from the same measurement. |

### T5 · Grouping requested

| | |
|---|---|
| **Condition** | `has_grouping` |
| **Source** | `QueryRequirements` |
| **Reason** | Group counts that do not sum to the population indicate multiplication; a second route makes that visible. |
| **Risk prevented** | Silently inflated breakdowns. |
| **Corpus** | **4 of 42** |

### T6 · Ratio / percentage

| | |
|---|---|
| **Condition** | `has_ratio` |
| **Source** | `QueryRequirements` |
| **Reason** | Two populations and a denominator; a wrong denominator is invisible in the output. |
| **Risk prevented** | The 5.479%-vs-24.658% class, where AGE chose a different relationship. |
| **Corpus** | **3 of 42** |
| **Caveat** | AGE cannot compute ratios. Verification here means **semantic corroboration only** — the trigger must not imply AGE will produce a comparable number. |

### T7 · Profile escalated or corrected

| | |
|---|---|
| **Condition** | `SelectionOutcome.escalated` or `.corrected` |
| **Source** | `gates.validate_selection()` — pre-execution |
| **Reason** | The LLM's own reading of the query disagreed with the derived requirement. That disagreement is evidence of ambiguity. |
| **Risk prevented** | Acting on a misread question without a second opinion. |
| **Corpus** | 4 escalated + 6 corrected in the last run |

### T8 · Novel spec shape

| | |
|---|---|
| **Condition** | structural shape not among those the corpus has validated |
| **Source** | shape tuple `(measure, entity, traversal_count, predicate_count, grouping, ratio, window, grain)` — the corpus exhibits **24 distinct shapes across 42 cases** |
| **Reason** | Exposing the full algebra widens the input distribution far faster than validation evidence widens. 3 of 9 `FilterOp`s, 1 of 5 grains beyond ENTITY, and zero 2-hop traversals have ever been exercised. |
| **Risk prevented** | A compiler path reaching production having never been run. |
| **Open** | requires a stored shape inventory — see §6 Q1. |

### Trigger reach (measured)

| Trigger | Corpus cases |
|---|---:|
| T1 multi-source | 12 |
| T2 traversal | 9 |
| T4 sparse field | 6 |
| T5 grouping | 4 |
| T6 ratio | 3 |
| **No trigger fires** | **10** |

Roughly **24% of the corpus is structured-only eligible** under this policy.
The rest verifies. That ratio is the policy's cost profile: three-quarters
of questions pay the AGE latency.

---

## 2. Optional signals — LLM may recommend, rules do not force

The planner may request verification on these; trusted code neither
requires nor forbids it.

| Signal | Why optional |
|---|---|
| **Question ambiguity** — undefined terms ("serious", "recent", "criminals") | The model is genuinely better placed to notice an unmapped term. But it cannot be trusted to notice *absence* reliably, so it is a recommendation only. |
| **Domain-term uncertainty** — "malkhana", "zimni", "FIR" | Resolvable or not; a wrong mapping is silent. |
| **Stakes stated by the user** ("for a court filing") | Legitimate reason to want corroboration; not derivable from the spec. |
| **Multiple plausible interpretations** | The model can say it hesitated between two populations. |
| **Unfamiliar phrasing** | Weak signal, cheap to honour. |

**Invariant:** the LLM may only *raise* the verification level, never lower
it. `effective_level = max(deterministic_level, llm_recommendation)` — the
same shape as the approved gate-profile escalation rule, and it reuses that
precedent rather than inventing a second policy.

---

## 3. Structured-only is safe when ALL hold

1. `requires_multiple_sources` is false — one authority answers everything
2. `traversal_count == 0` — no fanout, no grain ambiguity
3. No sparse field (every referenced property ≥ 50% presence)
4. No grouping, no ratio
5. Profile was accepted exactly — not escalated, not corrected
6. Shape is in the validated inventory
7. `ValidationResult.ok` — the spec validates cleanly

**Profile:** a direct count over a single label with a dense property, at
ENTITY grain, no traversal — `SIMPLE_COUNT` or `FILTERED_COUNT`.

**Examples that qualify:** *How many cases are registered?* (73) ·
*How many police stations?* (19) · *How many documents?* (1012)

**Examples that do not:** *How many persons aged 20–30 in 2024 cases?*
(T1 + T2 + T4) · *How many cases per station?* (T5) · *What percentage…*
(T6).

---

## 4. Reuse of existing abstractions

**Reused as-is — no new concepts:**

| Existing | Used for |
|---|---|
| `QueryRequirements` (8 fields) | T1, T2, T5, T6 — already derived pre-execution |
| `SelectionOutcome.escalated/.corrected` | T7 |
| `EntityInfo.property_presence()` | T4 |
| `RelationshipInfo.fans_in_direction()` | T3 |
| `ValidationResult.ok` | safe-path condition 7 |
| `ValidationIssue(code, message, field)` | recording why verification was forced |
| `AggregateSpec.spec_hash()` | shape inventory keying (T8) |

**Deliberately NOT reused as triggers:** `CoverageVerdict` and
`GateEvaluation` — both post-execution (§0). T4 substitutes registry
presence, which is the same underlying measurement available earlier.

**No new class is proposed.** A verification level is a small enum plus a
tuple of `ValidationIssue`s explaining it — both existing shapes.

---

## 5. Decision flow

```
typed AggregateSpec
        ↓
validate()                          ── not ok → refuse (no verification question)
        ↓
requirements_from_spec()            ← registry
validate_selection()                ← LLM profile recommendation
        ↓
evaluate T1…T8  (deterministic, pre-execution)
        ↓
   any fired? ──── yes ──→ VERIFY  (structured + AGE + semantic)
        │ no
        ↓
   LLM recommends verification? ─ yes ──→ VERIFY  (raise only)
        │ no
        ↓
   STRUCTURED-ONLY
        ↓
execute → gates → coverage → (reconcile, if verified)
```

Deterministic rules are evaluated **before** the LLM recommendation is
read, so the recommendation cannot influence them.

---

## 6. Risks and open questions

| # | Risk | Severity | Note |
|---|---|---|---|
| R1 | **76% of questions trigger verification**, each costing 41–92 s | **HIGH** | The policy is correct but expensive. Mitigation is execution strategy (async/deferred), not weaker triggers. |
| R2 | T8 needs a shape inventory that does not exist | MEDIUM | Q1 |
| R3 | T6 implies AGE verification it cannot provide | MEDIUM | Ratio verification is semantic-only; must be explicit or the result looks unverified. |
| R4 | Sparse threshold of 50% is **unvalidated** | MEDIUM | Q2 — chosen to match `Person.age` at 4.4% and `gender` at 27.7%; no evidence 50% is the right line. |
| R5 | Triggers are independent booleans | LOW | No interaction weighting; deliberate — simple and auditable. |

**Open questions**

- **Q1** Where does the validated-shape inventory live, and who updates it
  when the corpus grows? Without an answer T8 cannot ship.
- **Q2** Is 50% the right sparsity threshold? A measured pass over the
  corpus would settle it; today it is a judgement.
- **Q3** When verification is mandatory but AGE is *unavailable* (ratio,
  median, time-window), does the answer ship marked unverified, or refuse?
  This is conflict-case B1, still open from the prior review.
- **Q4** Does the LLM see the deterministic outcome before recommending?
  Showing it invites anchoring; hiding it wastes a recommendation on
  already-verified queries. Recommend hiding.
- **Q5** Should a prior CONFLICT on the same `spec_hash` force verification
  on later runs? Requires persistence nothing currently has.

---

## 7. Summary

The policy is buildable **entirely from signals that already exist**, with
one exception (T8's shape inventory). No new abstraction is required.

The uncomfortable finding is R1: applied honestly, these triggers verify
about three-quarters of the corpus. The triggers are not over-broad — each
maps to a defect measured in this system — so the cost belongs to the
execution strategy decision, not to loosening the rules. Weakening a
trigger to buy latency would be trading a known correctness guarantee for
speed, which is the trade this architecture spent eight phases refusing.
