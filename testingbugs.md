# testingbugs.md — remediation plan for the 50-question route sweep

Working document for fixing what the 27 Aug 2026 live-fire route sweep found
(50 questions, all 9 routes, run against the local D:\ deployment through the
real `POST /api/chat` SSE endpoint as `platform-admin`).

Every claim below was re-verified against the live Postgres / Apache AGE graph
**after** commits `a32073b`, `09bed41`, `ea2ae93` landed. Where a claim from the
original sweep report turned out to be wrong, it is retracted here explicitly
rather than quietly dropped — see [§2](#2-corrections-to-the-original-report).

- [1. Status board](#1-status-board)
- [2. Corrections to the original report](#2-corrections-to-the-original-report)
- [3. BUG-1 — stale `case_id` crashes the turn (Critical)](#3-bug-1--stale-case_id-crashes-the-turn-critical)
- [4. BUG-2 — `route_result` UnboundLocalError kills the turn (High)](#4-bug-2--route_result-unboundlocalerror-kills-the-turn-high)
- [5. BUG-3 — orphaned entity nodes break GRAPH seeding (High)](#5-bug-3--orphaned-entity-nodes-break-graph-seeding-high)
- [6. BUG-4 — evaluator rejects valid graph evidence (Medium-High)](#6-bug-4--evaluator-rejects-valid-graph-evidence-medium-high)
- [7. BUG-5 — router prompt is over budget for the Groq TPM cap (Medium)](#7-bug-5--router-prompt-is-over-budget-for-the-groq-tpm-cap-medium)
- [8. BUG-6 — docs describe a dataset that isn't loaded (Low)](#8-bug-6--docs-describe-a-dataset-that-isnt-loaded-low)
- [9. Explicitly out of scope](#9-explicitly-out-of-scope)
- [10. Execution order](#10-execution-order)
- [11. Regression tests to add](#11-regression-tests-to-add)
- [12. Repro commands](#12-repro-commands)

---

## 1. Status board

| # | Issue | Severity | Status |
|---|---|---|---|
| — | Duplicate Person node explosion (141 nodes / 1 person / 1 case) | Critical | **Fixed** by `ea2ae93` — verified, 2 active Person ids on `fir-1001-26` |
| — | XGRAPH "false negative" for کاشف | Critical (claimed) | **Retracted** — system was correct, my test was invalid (§2) |
| BUG-1 | Stale/foreign `case_id` → FK violation → HTTP 500 | Critical | **Fixed** on `fix/case-validation-and-router-crash` — verified live: 404, no stack trace |
| BUG-2 | `route_result` UnboundLocalError swallows the whole turn | High | **Fixed** on `fix/case-validation-and-router-crash` — verified live: stream completes, real answer delivered |
| BUG-3 | Orphaned Vehicle/Officer nodes → GRAPH "no seed entity matched" | High | Open |
| BUG-4 | Evaluator rejects valid graph evidence → falls back to RAG, then fails | Medium-High | Open |
| BUG-5 | Router system prompt + budget exceeds Groq 8000 TPM cap | Medium | Open |
| BUG-6 | `RUN.md` / fixture CSVs describe a dataset not in this DB | Low | Open |
| — | No global/citizen-service docs in KB | — | **Out of scope** (owner: not a bug) |
| — | XAGG can't filter free-text `investigation_status` | — | **Out of scope** (working as designed, §9) |

---

## 2. Corrections to the original report

Two claims in the published sweep report need correcting. Both were mine, and
both were wrong in the same direction — asserting a system fault where the
system was actually behaving correctly.

### 2.1 The XGRAPH "false negative" was not a false negative

The report claimed XGRAPH wrongly answered *"No connections to other cases were
found"* for کاشف, who "demonstrably recurs across 8 cases." That conclusion was
wrong. Re-checking the identity key rather than the display name:

```
fir-1001-26  PERSON-000db746bc  CNIC 00000-9000058-1
fir-204-26   PERSON-5f1603c79f  CNIC 00000-9000060-1
fir-205-26   PERSON-1d0633635e  CNIC 00000-9000018-1
fir-210-26   PERSON-5d5bd99ac2  CNIC 00000-9000027-1
fir-213-26   PERSON-15d51adf26  CNIC 00000-9000035-1
fir-216-26   PERSON-b12f0cf647  CNIC 00000-9000046-1
fir-217-26   PERSON-c8c52f0a6e  CNIC 00000-9000051-1
fir-466-26   PERSON-aaf0a91dd5  CNIC 00000-9000210-1
```

**Eight distinct CNICs.** These are eight different people who share a very
common Pakistani given name. Entity resolution correctly refused to link them,
and XGRAPH correctly reported no cross-case identity. The test question was
invalid: I picked a bare first name with no disambiguator as a "recurring
entity" probe. **No fix required. Do not "fix" this.**

> **Lesson for future sweeps:** seed cross-case recurrence tests from a
> *hard identity key* (CNIC / plate / phone) confirmed to repeat in the graph,
> never from a display name. See [§11](#11-regression-tests-to-add).

### 2.2 The duplicate-node finding was real, and is now fixed

The other half of the same finding — 141 Person nodes for one person in one
case — was real. `ea2ae93` fixed it properly (append-only supersede, donors
tagged not deleted, CNIC-conflict re-check before merge). Verified live:

```
DISTINCT active (non-merged) Person entity_ids on fir-1001-26: 2
  PERSON-000db746bc  کاشف
  PERSON-0107e37f13  فیصل
```

Note this fixed the **data**, not the routes that surfaced it. `a32073b`'s
canonicalization went into `xagg.py` and the harness `local_search.py` only —
`src/retrieval/graph_retriever.py`, which serves the GRAPH and XGRAPH routes,
was not touched (`git diff --stat HEAD~3 HEAD` confirms). That is fine given
§2.1, but it is worth knowing the two routes have no canonicalization layer if
a genuine same-CNIC duplicate ever does appear across cases.

---

## 3. BUG-1 — stale `case_id` crashes the turn (Critical)

### Symptom
Any chat turn carrying a `case_id` not present in `cases` returns HTTP 500 with
an empty body. The pipeline never runs. Server log shows a raw FK violation.

### Verified evidence
```
sqlalchemy.exc.IntegrityError: insert or update on table "sessions" violates
foreign key constraint "sessions_case_id_fkey"
DETAIL: Key (case_id)=(CASE-DRY-001) is not present in table "cases".
```

### Root cause (verified)
Two things compound:

1. `src/data_gateway/direct_backend.py:614-616` — `check_case_access()` returns
   `True` immediately for `platform-admin` **without ever checking the case
   exists**:
   ```python
   async def check_case_access(self, case_id, user_id, user_role, min_role=None):
       if user_role == "platform-admin":
           return True
   ```
   For a non-admin the missing assignment yields `False` → a 403, which at
   least doesn't crash (though it's a misleading message for a nonexistent
   case). For an admin, the bogus id sails straight through.

2. `src/main.py:369-371` — the resolved `case_id` then goes directly into
   `gateway.create_session(...)`, whose INSERT hits the FK constraint. There is
   no existence check anywhere between the two.

### Fix
`src/data_gateway/direct_backend.py:594` already provides `get_case(case_id)`.
Validate before use, in the endpoint, so *every* role gets the same answer:

In `src/main.py`, immediately before the `check_case_access` call at line ~356:

```python
if case_id:
    if await gateway.get_case(case_id) is None:
        raise HTTPException(status_code=404, detail="Case not found")
    if not await gateway.check_case_access(case_id, user_id, current_user.role):
        raise HTTPException(status_code=403, detail="Not assigned to this case")
```

Order matters and is deliberate: 404-before-403 is correct here because case
ids are not secret in this system (they appear in FIR numbers, document ids,
and cross-case answers), so distinguishing "doesn't exist" from "not yours"
leaks nothing an investigator can't already see, and it turns a silent 500 into
an actionable message. If a future threat model says otherwise, invert to
return 403 for both — but do it as a deliberate decision, not by leaving the
500 in place.

Also fix the same gap on the session-resume path: a session row can carry a
`case_id` for a case deleted since (`src/main.py:351` reads `session.get("case_id")`).
The check above covers it because it runs on the *resolved* `case_id`, not just
the request field — keep it that way.

### Verification
```bash
# should be 404, not 500, and not 403
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:8000/api/chat \
  -b cookies.txt -H "X-CSRF-Token: $CSRF" -H "Content-Type: application/json" \
  -d '{"session_id":"'$(uuidgen)'","message":"hi","case_id":"CASE-DOES-NOT-EXIST"}'
```
Then confirm a *valid* case_id still returns 200 and streams normally, and that
a non-admin on an unassigned-but-real case still gets 403.

### Risk
Low. Adds one indexed PK lookup per case-scoped turn. No schema change.

---

## 4. BUG-2 — `route_result` UnboundLocalError kills the turn (High)

### Symptom
When the router raises, the client receives **no events at all** — not even the
degraded-RAG answer the except branch was written to produce.

### Verified evidence
```
router: Error code: 413 - 'Request too large ... tokens per minute (TPM): Limit 8000, Requested 9261'
system: cannot access local variable 'route_result' where it is not associated with a value
```

### Root cause (verified, exact line)
`src/pipeline/orchestrator.py:893-904` — the `except Exception` branch carefully
assigns every fallback variable (`route_str`, `output_format`, `case_scope`,
`target_entity`, `secondary_methods`, `router_confidence`, `router_station`,
`router_district`, `elapsed_ms`) **except `route_result`**.

Then at `src/pipeline/orchestrator.py:975`, outside the try:
```python
yield event(
    "router", "done", f"Route decided: {route_str}", elapsed_ms,
    confidence=router_confidence, case_scope=case_scope,
    reason=route_result.get("reason"),   # ← UnboundLocalError on the error path
)
```
The generator dies mid-stream, so the SSE connection closes with nothing
useful written. The router's own fallback-to-RAG design is completely defeated
by a one-variable oversight.

### Fix
Assign `route_result` in the except branch alongside its siblings — matching
the shape `route_query()` itself returns on failure (`src/pipeline/router.py:571-582`):

```python
except Exception as exc:
    logger.error("Router failed: %s", exc)
    yield event("router", "error", str(exc))
    route_result = {"reason": f"Router failed ({type(exc).__name__}), defaulting to RAG"}
    route_str = "RAG"
    ...
```

Defensive hardening in the same edit (cheap, prevents the whole class):
change line 975 to `reason=(route_result or {}).get("reason")` so a future
edit that forgets again degrades to `None` instead of killing the stream.

### Verification
Temporarily force the failure (e.g. monkeypatch `route_query` to raise) and
confirm the turn still streams `router:error` → `router:done` → a RAG-grounded
or safe-fallback answer, and the client receives a complete response. Then
re-run sweep Q21 (`"What is the latest press release from Islamabad Police?"`)
and confirm it now returns an answer rather than nothing.

### Risk
Very low — strictly widens an existing error path.

---

## 5. BUG-3 — orphaned entity nodes break GRAPH seeding (High)

### Symptom
GRAPH queries anchored on a vehicle return
`"Graph traversal found no connected evidence (no seed entity matched)"`, then
fall back to RAG and fail. Sweep Q27 (`ICT-LE-309`) reproduces this reliably,
before *and* after the three commits.

### Verified evidence
Node exists, with perfect extraction confidence, and has **zero edges of any kind**:
```
MATCH (v:Vehicle) WHERE v.plate='ICT-LE-309'
OPTIONAL MATCH (v)-[r]-() RETURN v.entity_id, type(r)
→ VEHICLE-3a1c7c67c6, rel = None
```

Corpus-wide scope — this is not one bad node:
```
Vehicle    total=2      with_BELONGS_TO_CASE=0      orphaned=2     (100%)
Officer    total=1030   with_BELONGS_TO_CASE=138    orphaned=892   (86.6%)
Weapon     total=32     with_BELONGS_TO_CASE=32     orphaned=0
Person     total=429    with_BELONGS_TO_CASE=429    orphaned=0
```

### Why this breaks seeding
`src/retrieval/graph_retriever.py:285-365` (`_find_seed_nodes`) resolves every
within-case seed through a mandatory case hop:
```cypher
MATCH (n:{label})-[:BELONGS_TO_CASE]->(c:Case {case_id: $case_id})
WHERE toLower(n.{prop}) CONTAINS toLower($cand)
```
A node with no `BELONGS_TO_CASE` edge is unreachable by design — correctly so,
since that edge is the case-scoping security boundary (`scoped_cypher`). The
node isn't missing; it's unlinked. `Vehicle` and `Officer` are both in
`_SEED_LABELS` (`graph_retriever.py:129-149`), so both are affected.

### Root cause — NOT yet established, diagnose first
This needs a diagnostic step before any code change, because the projection
code *looks* correct:

- `src/graph/entity_resolution.py:680-684` — `resolve_and_write()` writes
  `BELONGS_TO_CASE` **unconditionally** for every label.
- Both officer paths (`structured_projection.py:481`, `:521`) and the
  malkhana vehicle/phone path (`structured_projection.py:1104`) call
  `resolve_and_write()`.

So on a clean run these edges *should* exist. That the Vehicle node has no
`APPEARS_IN` edge either — which `_classify_and_write_malkhana_item` writes
explicitly at `structured_projection.py:1106-1110`, in addition to the two
`resolve_and_write` writes — points away from "one missing write" and toward
**every edge write for that node failing at runtime and being swallowed**.

Candidate causes, in order of likelihood:
1. **Swallowed exception.** Callers wrap writes in
   `except Exception as exc: logger.warning(...)` (e.g.
   `structured_projection.py:1156-1158`). An AGE elabel/vlabel that didn't
   exist at first write (the exact race migrations 020/023/024 pre-create
   labels to prevent) would fail here silently. `Vehicle` has **no**
   pre-creation migration — 023 covers PoliceStation/District/FILED_AT, 024
   covers Officer/ASSIGNED_TO, neither covers Vehicle.
2. **Historical artifacts.** These nodes may predate the current projection
   code (ICT-LE-309 is also a `data/memory` fixture plate — see BUG-6), in
   which case the live code is fine and only a backfill is needed.
3. A genuine gap in a path not yet read.

**Diagnostic (do this first, ~30 min):**
```bash
# a) Do the orphans share a source_doc_id / ingestion window?
#    Group orphaned Officer nodes by source_doc_id and as_of date.
# b) Re-ingest ONE fir into a scratch graph and check whether a fresh
#    Vehicle/Officer node gets its BELONGS_TO_CASE edge.
# c) Grep ingestion logs for swallowed warnings:
grep -i "write failed\|edges_written\|label.*does not exist" backend*.log
```
The answer to (b) decides everything: if a fresh ingest links correctly, this
is **backfill-only** (no code change). If it doesn't, fix the write path first.

### Fix
Split by what the diagnostic finds.

**If code path is broken** — add the missing/failing write and stop swallowing:
- Add a `Vehicle`/`PhoneNumber` vlabel pre-creation migration mirroring
  `023_jurisdiction_graph_labels.sql` / `024_officer_graph_labels.sql`.
- Change the swallowing handlers to record into `stats["errors"]` **and**
  surface a non-zero exit / warning count at the end of an ingestion run, so a
  silent 100%-failure rate can never look like success again. This is the part
  worth doing regardless of the diagnostic result — a projection that loses
  every Vehicle edge and still reports a clean run is the real defect here.

**Backfill (needed either way)** — new
`scripts/backfill_missing_belongs_to_case.py`, following the exact shape of the
existing graph-mutation scripts (`--dry-run` / `--apply` / `--admin-email`,
append-only via `versioning.write_edge()`, never a raw insert):
- For each entity node with no active `BELONGS_TO_CASE`, derive its case from
  `source_doc_id` (the `documents` table maps `doc_id → case_id`; the Vehicle's
  `psrms/fir/fir-1001-26#structured` already encodes it) and write the edge.
- Report — do not guess — any node whose case cannot be derived unambiguously.
- Take a `pg_dump` first, same as `ea2ae93` did.

### Verification
```
MATCH (n:Vehicle)-[:BELONGS_TO_CASE]->(c:Case) RETURN count(DISTINCT n)   → 2
MATCH (n:Officer)-[:BELONGS_TO_CASE]->(c:Case) RETURN count(DISTINCT n)   → 1030
```
Then re-run sweep **Q27** and confirm it produces a real timeline instead of
"no seed entity matched".

### Risk
Medium — mutates graph data. Mitigated by dry-run + backup + append-only
semantics. Note the 1030 Officer nodes for 73 cases also smells like the same
duplication class `ea2ae93` fixed for Person; **do not** merge Officers as part
of this bug — file it separately once the orphan edges are restored, so the two
changes stay independently reviewable and revertible.

---

## 6. BUG-4 — evaluator rejects valid graph evidence (Medium-High)

### Symptom
`"Graph results judged not relevant. Falling back to RAG."` → then
`"Max retries (2) reached — no sufficient evidence found"` → user gets nothing.
6 of 8 GRAPH questions in the sweep ended this way.

### Verified evidence
Sweep Q24 (`"Is کاشف known to associate with anyone else in this case?"`,
`case_id=fir-1001-26`) **still fails identically after `ea2ae93`**, re-tested
live on the cleaned graph:
```
[24] (GRAPH) -> got=GRAPH [OK] 179.96s
     retrieval: Graph results judged not relevant. Falling back to RAG.
     evaluator: Max retries (2) reached — no sufficient evidence found
```
This is decisive: `fir-1001-26` now has exactly 2 clean, linked, non-duplicated
Person nodes (کاشف and فیصل), both with CNICs — the ideal input — and the
seed lookup can reach them. So this is **not** a data-quality failure and not a
seeding failure. It is the relevance judgement at
`src/pipeline/orchestrator.py:1378-1389` rejecting good evidence.

Corroborating: the *same case, same facts* answered correctly on two other
routes in the same sweep — plain RAG (Q15, full cited FIR summary naming both
people) and GRAPH_HYBRID (Q32, correct "connected to the accused" answer). Only
the standalone GRAPH path fails, and it fails at the evaluator.

### Root cause — hypothesis, needs confirmation
`evaluate_relevance(user_message, rewritten_query, reranked)` is being handed
the graph route's **synthetic evidence chunks**
(`graph_retriever.py:893` `_synthetic_evidence_chunk`), which are terse
machine-generated sentences, not prose document chunks. The evaluator prompt
was tuned on document chunks. Suspicion: it scores synthetic relationship
sentences as irrelevant because they don't read like retrieved text.

**Diagnostic (do this first):**
Log the actual `reranked` payload and the evaluator's `reason` string for Q24.
The `evaluator:done` event already carries a truncated reason —
capture it untruncated:
```python
yield event("evaluator", "done", f"Relevant: {is_relevant} — {evaluation.get('reason','')[:60]}")
```
Raise that slice (or log it in full server-side) and run Q24 once. The reason
text will say whether it's rejecting the *content* or the *form*.

### Fix (choose after diagnosis)
- **If the evaluator misjudges synthetic chunks:** give the graph route its own
  evaluator prompt variant that states the evidence is machine-extracted
  relationship assertions, or skip the evaluator gate when the only candidates
  are synthetic graph chunks and the traversal *did* return seeds (a traversal
  that found real edges has already established relevance — the evaluator is
  arguably redundant there).
- **If the evidence really is thin:** the graph route should say *"the graph
  shows X and Y in this case with no recorded relationship between them"* —
  a true, useful answer — instead of falling through to a RAG retry that then
  reports total failure. That is a better terminal state than the current
  double-failure.

Either way the current behaviour is the worst of both: it discards good graph
evidence *and* reports "no information" for a case whose FIR the same system
summarises correctly on another route.

### Verification
Q24, Q26, Q29, Q30 all produce grounded answers naming the case's real people.
Q31 and Q28 (which already pass) must not regress.

### Risk
Medium. Loosening a relevance gate can let weak evidence through — pair any
change with the existing `verify_grounding()` check, which stays as the
backstop, and re-run the full 50-question sweep before/after.

---

## 7. BUG-5 — router prompt is over budget for the Groq TPM cap (Medium)

### Symptom
`413 rate_limit_exceeded — Limit 8000, Requested 9261` on the router call.
Intermittent; hit once in 50 questions.

### Root cause
`prompts/router.txt` is ~7.6k tokens of system prompt on its own. With the
query, history and the reserved completion budget, a single router call can
exceed the account's 8000 TPM `on_demand` ceiling. `router.py:365-387` already
documents fighting this exact limit (`cloud_max_tokens=300`,
`reasoning_effort="low"` were both added for it) — the headroom is simply gone.

### Fix — pick one, in preference order
1. **Raise the ceiling.** Groq Dev Tier. Cheapest fix by engineering hours;
   no code risk. Requires a billing decision — flagging, not deciding.
2. **Shrink the prompt.** `router.txt` carries ~45 few-shot examples, several
   near-duplicates (three separate "list all people in the cases" phrasings,
   two identical `"Give me a full picture..."` GRAPH_HYBRID examples). The
   deterministic regex overrides in `router.py:63-164` already handle most of
   the XAGG/XGRAPH/XNETWORK/SQL cases these examples teach, so a chunk of the
   prompt is paying rent twice. Trimming to ~25 examples should reclaim ~2-3k
   tokens. **Must be validated by re-running the full sweep** — this prompt's
   examples are load-bearing and hard-won.
3. **Backstop regardless:** BUG-2's fix already turns this from "turn dies" into
   "degrades to RAG". Ship BUG-2 first and this drops to a quality issue.

### Verification
Re-run the sweep; zero 413s across 50 questions, and route accuracy does not
drop below the 47/50 baseline (excluding the two corrections in §2).

---

## 8. BUG-6 — docs describe a dataset that isn't loaded (Low)

### Symptom
`RUN.md` §7 and `data/memory/case_index.csv` / `entity_roster.csv` describe a
20-case synthetic Islamabad dataset (`CASE-DRY-001`, `CASE-002`, …). **None of
those case ids exist in this database.** The live DB holds 73 `fir-XXXX-YY`
cases from the Muhafiz API migration, spanning Lahore, Karachi, Faisalabad,
Rawalpindi, Hyderabad and Chiniot — not Islamabad-only as the router prompt's
persona assumes.

Anyone following RUN.md's worked examples verbatim triggers BUG-1 immediately.
This is how BUG-1 was found.

### Fix
Documentation only:
- Add a note at the top of `RUN.md` §7 stating the bundled `data/memory`
  fixtures are **not** what's loaded in a Muhafiz-API-migrated deployment, and
  how to list the real case ids (`SELECT case_id FROM cases ORDER BY case_id`).
- Consider whether the Islamabad-specific framing in `prompts/router.txt` and
  the DIRECT persona still matches a multi-city corpus. Sweep Q3/Q4/Q5 show the
  assistant introducing itself as Islamabad-only while serving Lahore and
  Karachi cases. Not a bug; a positioning question for the owner.

### Risk
None.

---

## 9. Explicitly out of scope

**Global / citizen-service KB content.** Sweep Q12, Q13, Q14, Q18 (FIR copy
procedure, tenant registration fee, lost-item report) all failed. Confirmed
cause: the live ChromaDB collection holds 790 chunks, **0** with
`is_global: true` — every chunk is a case-scoped operational record. Owner has
confirmed these documents are simply not in the database and this is expected.
**No action. Do not ingest, do not change retrieval.**

**XAGG free-text `investigation_status`.** Sweep Q42/Q45 couldn't filter by
open/closed. This is **working as designed** and should not be "fixed" in the
pipeline. `src/pipeline/xagg.py:184-194` (`_status_filter_supported()`) already
detects that the corpus carries no parseable status and emits an explicit
caveat instead of a confidently wrong number:

```
"Case status could not be filtered: the case records in this corpus do not
 carry a structured open/closed status, so the figures below cover all
 matching cases regardless of status."
```

The code comments state it self-heals if a future corpus does carry parseable
statuses. Data reality: `investigation_status` is NULL for 52 of 73 cases, and
the other 21 are each a unique free-text Urdu sentence
(`"دونوں ملزمان ریمانڈ پر، فرانزک رپورٹ کا انتظار"`). If open/closed
aggregates are wanted, that is an **ingestion-layer** change (normalise status
into a structured column at projection time) and a separate piece of work —
not a bug in XAGG.

---

## 10. Execution order

Sequenced so each step is independently shippable and revertible.

| Step | Work | Why this order |
|---|---|---|
| 1 | **BUG-2** (`route_result`) | Smallest, safest, pure widening of an error path. Also downgrades BUG-5 from fatal to cosmetic. |
| 2 | **BUG-1** (case existence check) | Small, self-contained, removes a Critical 500. Independent of everything else. |
| 3 | **BUG-3 diagnostic** | Decides whether BUG-3 is a code fix or backfill-only. No changes yet. |
| 4 | **BUG-3 fix + backfill** | Biggest data risk — do it alone, after a `pg_dump`, with dry-run first. |
| 5 | **BUG-4 diagnostic + fix** | Depends on step 4: with orphans restored, some GRAPH failures may resolve on their own. Re-measure before changing the evaluator. |
| 6 | **BUG-5** | Only if step 1's backstop is judged insufficient. Prompt trimming needs a full sweep to validate. |
| 7 | **BUG-6** | Docs. Any time. |
| 8 | **Full 50-question re-sweep** | Compare against the 27 Aug baseline. |

Steps 1, 2 and 7 are safe to do in one sitting. Step 4 wants its own session.

---

## 11. Regression tests to add

- `tests/test_chat_case_validation.py` — bogus `case_id` returns 404 for
  platform-admin *and* for investigator; valid-but-unassigned returns 403;
  valid-and-assigned returns 200.
- `tests/test_orchestrator_router_failure.py` — `route_query` raising must
  still stream a complete turn (asserts the BUG-2 regression directly).
- `tests/test_graph_seed_requires_case_edge.py` — a node without
  `BELONGS_TO_CASE` is not seedable; after the backfill writes the edge, it is.
- **Sweep harness fix** — the cross-case recurrence questions must seed from a
  CNIC/plate/phone verified to repeat in the graph, not a display name (§2.1).
  Query the graph for a genuinely recurring hard identifier first, and skip the
  test with a clear message if none exists rather than asserting a false
  positive.

---

## 12. Repro commands

Harness and results from the original sweep:
```
scratchpad/run_questionnaire.py     # driver (login + CSRF + SSE parse)
scratchpad/questions.json           # the 50 questions
scratchpad/results.jsonl            # full run output
```

Re-run a subset by id:
```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe run_questionnaire.py 21 24 27 39
```

Graph checks used throughout this document:
```python
# active (non-merged) persons on a case
MATCH (p:Person)-[:BELONGS_TO_CASE]->(c:Case {case_id:'fir-1001-26'})
WHERE p.merged_into IS NULL RETURN DISTINCT p.entity_id, p.canonical_name

# orphan census, per label
MATCH (n:Vehicle) RETURN count(n)
MATCH (n:Vehicle)-[:BELONGS_TO_CASE]->(c:Case) RETURN count(DISTINCT n)
```

> AGE note: `WHERE NOT (n)-[:X]->(:Label)` fails to parse. Count total and
> linked separately and subtract, as above.

---

*Baseline: 27 Aug 2026 sweep — 48/50 routed correctly, 29/50 evidence-grounded
answers, 52.7s mean latency. Re-measure all three after step 8.*
