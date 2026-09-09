# Module 92 — the router generalises unevenly across languages

**Branch:** `fix/router-language-generalisation` · **Found by:** Modules 62
(CR3/G6), 78 (KB3/KB9), 88 (CR2), each filing the same shape independently.

**Headline, stated before the detail, because it is not the result the brief
expected:**

1. The defect is real, and the plan's framing of it was too narrow. Measured
   over 96 paraphrases rather than 8, the deterministic override layer barely
   generalises in *any* language (English 19%, Roman-Urdu 25%, Urdu 25%), and
   the language gap that matters lives one layer down, in the LLM classifier.
2. All three candidate directions were built and measured. The best —
   the compact prompt on a **cloud** model — is the only one that makes the
   three languages equal (**69% / 69% / 69%**, against the shipped
   59% / 50% / 62%). It is blocked on infrastructure, not on code.
3. **The candidate that won on route accuracy lost on answers, and that is
   this module's most useful finding.** Sending the compact prompt to the
   *local* model scored 55/96 → 61/96 with *nothing* regressing on route
   labels — and when the same five questions were then run end-to-end, two
   substantive answers had become "no sufficiently relevant documents" and one
   question reached DIRECT and invented an ungrounded note. **Route accuracy
   is not a proxy for answer quality.** It did not ship.
4. What shipped is the part that survived that test: a **latent-path bug fix**
   with a measured zero blast radius. `route_query()`'s local path is
   byte-identical on all 32 gold questions across all 8 load-bearing fields;
   its documented cloud escalation, which had been returning HTTP 413 silently
   for weeks, now works.

**The language-generalisation defect itself is NOT fixed. Module 92 stays
open.** The numbers for what would fix it, and the exact blocker, are in §1.4.

---

## 1. Root cause

### 1.1 The probe, re-run to confirm

`docs/gold-qa-wave2-results/module92_override_survival_probe.py`, unchanged,
on `origin/main` at the time this module started:

```
Q          gold   en-para   ru-para
D1         XAGG      XAGG       LLM
CR2        XAGG      XAGG       LLM
CP1        XAGG       LLM       LLM
M2         XAGG       LLM       LLM
CS4        XAGG      XAGG       LLM
S2         XAGG      XAGG       LLM
CR4        XAGG       LLM       LLM
M1         XAGG      XAGG       LLM

English paraphrase keeps gold's route:    5/8
Roman-Urdu paraphrase keeps gold's route: 0/8
```

Reproduced exactly. The override-coverage figures the plan records also
reproduce exactly: **20 of 32** gold questions take a deterministic override,
**en 6/11, roman-ur 6/10, ur 8/11**.

### 1.2 What the probe's framing misses

The probe scores the **regex layer alone** and counts a fall-through to the
classifier as a failure. Over the full 96-paraphrase corpus that layer scores
**en 19%, roman-ur 25%, ur 25%** — it barely generalises anywhere, and English
has no real advantage. The 5/8-vs-0/8 split came from eight questions whose
English patterns happen to be broad.

The layer that produces the *language* asymmetry is the classifier the misses
fall through to:

| | paraphrases keeping the gold route | en | roman-ur | ur |
|---|---|---|---|---|
| deterministic layer alone | 22/96 (23%) | 19% | 25% | 25% |
| **shipped router (override, else local Qwen3-14B)** | **55/96 (57%)** | **59%** | **50%** | **62%** |

So the shipped router recovers a great deal of what the regex layer misses —
Roman-Urdu is 50%, not 0% — and still has a **9-point** English/Roman-Urdu gap
on top of a ~40% overall miss rate.

Two things also fell out of establishing this baseline:

- **The query rewriter is not a confound.** `rewrite_query()` returns the
  message unchanged when `conversation_history` is empty, and
  `evaluation/gold32_run.py` sends a fresh `session_id` per question, so the
  gold routes were produced from the raw gold text. The harness is faithful.
- **Module 78's fix, which landed mid-module, is the right shape and the
  evidence for it.** It routes on `rag.py::_is_legal_kb_intent()` rather than
  on new vocabulary, and it lifted the deterministic layer from 10/96 to 22/96
  paraphrases *evenly across all three languages* (19%/25%/25%). A semantic
  gate generalises where a pattern list does not.

### 1.3 The corpus and the harness

- `evaluation/gold32_paraphrases.json` — 96 paraphrases, one English, one
  Roman-Urdu and one Urdu-script for each of the 32 gold questions. **All 96
  were written before any of them was run against anything, and none was
  adjusted afterwards.** Integrity checked programmatically: 32/32/32 by
  language, no paraphrase identical to its gold, no mojibake, every Urdu entry
  actually in Arabic script.
- `evaluation/router_paraphrase_harness.py` — scores any candidate classifier
  over 32 gold + 96 paraphrases, broken out by language, with a per-item cache.
  Ground truth is the `route` in `evaluation/gold32_pass{1,2,3}_outputs.json`,
  and the harness re-asserts Module 27's "stable 32/32" rather than assuming
  it. Two questions are named exceptions: Module 78 deliberately moved **KB3**
  (XNETWORK→RAG) and **KB9** (XAGG→RAG) after those artefacts were recorded.
- `evaluation/module92_route_dict_equality.py` — the all-32 equality control
  over the **whole** router output dict, not just `route` (§7).
- `evaluation/module92_inprocess_run.py` — end-to-end runs without occupying a
  backend (§4).

### 1.4 The design comparison

All arms measured on the same 128 items, post-Module-78 `main`.

| candidate | gold 32 | paraphrases 96 | en | roman-ur | ur | miss-path latency |
|---|---|---|---|---|---|---|
| deterministic layer only | 28 (88%) | 22 (23%) | 6/32 | 8/32 | 8/32 | 0.02 s |
| **shipped: local Qwen3-14B** | **29 (91%)** | **55 (57%)** | **19/32** | **16/32** | **20/32** | **4.25 s** |
| direction 3 — e5 kNN over route exemplars | 28 (88%) | 42 (44%) | 11/32 | 22/32 | 9/32 | 1.46 s |
| compact prompt, local model | 29 (91%) | 61 (64%) | 22/32 | 19/32 | 20/32 | 5.34 s |
| direction 2 — translate to English, then classify | 29 (91%) | 62 (65%) | 19/32 | 22/32 | 21/32 | 13.22 s |
| **direction 1 — compact prompt, cloud model** | **29 (91%)** | **66 (69%)** | **22/32** | **22/32** | **22/32** | 14.79 s |
| direction 1 as specified — Gemini | — | — | — | — | — | **unavailable, see below** |

**Direction 1 (a reliable classifier) is the winner and it is blocked.**

- **Gemini could not be measured at all.** All 95 miss-path items returned
  `401 UNAUTHENTICATED`. Probing the pool directly: `GEMINI_API_KEY` and
  `GEMINI_API_KEY_1` are the same key and return **429** (free tier, 20
  requests/day for `gemini-2.5-flash`); `GEMINI_API_KEY_2/_3/_4` return **401
  invalid credentials**. The rotate-on-429 logic therefore rotates a
  throttled-but-valid key into three dead ones. Filed as defect 98.
- **Groq rejects the router prompt outright.** `prompts/router.txt` is
  **13,003 request tokens** against this account's **8,000** `on_demand`
  per-request cap: `"Request too large ... Limit 8000, Requested 13003"`. Not
  slow — a hard 413 before the model reads a token. This is why the cloud arm
  had to be measured with a compact prompt, and it is the bug §2 fixes.
- Even with the compact prompt, Groq sustains only **~2.7 router calls per
  minute** (≈2,900 tokens per request against 8,000 TPM). Putting that on the
  miss path of every query is not viable, and the quota is shared with every
  other agent on this machine. **This is a tier/quota problem, not a code
  problem.**

**Direction 2 (normalise to English first)** genuinely works and is the
cheapest way to lift Roman-Urdu (16/32 → 22/32) — but it *inverts* the gap
rather than closing it (English stays 19/32, because English queries gain
nothing from being translated) and it costs a second LLM round trip, tripling
miss-path latency.

**Direction 3 (embedding nearest-neighbour) is the clear loser, and the reason
is worth recording** because the plan expected it to be language-agnostic by
construction. It *is* — `multilingual-e5` scores the same question at 0.86–0.89
similarity across all three languages, verified live before anything was
concluded. But it is not *discriminative*: two unrelated police questions also
sit around 0.85, so route separation is noise. It is language-agnostically
wrong, and its margin does not correlate with correctness (D1's gold text
picks RAG with a 0.411 margin).

**And the ceiling is the real story.** Every arm that helps, helps by flattening
the languages — none lifts the overall paraphrase rate past ~69%. Roughly 30%
of ordinary rewordings are misrouted under every design measured.

---

## 2. The change

`src/pipeline/router.py` and `src/pipeline/json_extract.py`, plus a new
`prompts/router_compact.txt`.

- `call_llm_json()` gains an optional **`cloud_system_prompt`**, used only when
  an attempt actually goes to the cloud. Every existing caller omits it and
  gets exactly the previous behaviour — pinned by a test.
- `route_query()` passes `cloud_system_prompt=_CLOUD_SYSTEM_PROMPT`. **The
  local path still receives `_SYSTEM_PROMPT` (`router.txt`), unchanged.**
- `prompts/router_compact.txt` carries the same nine route definitions and the
  same 74 few-shot examples, compressed from a JSON object each to one
  `"query" -> ROUTE [non-default fields]` line, plus one rule router.txt only
  implies: *language is never a routing signal*. ~2,560 tokens, so a cloud
  router request is ~2,900 and is accepted.

**Live, before and after, one real call each** (`scratchpad/prove_escalation.py`,
Roman-Urdu query, `force_cloud=True`):

| | prompt size | outcome |
|---|---|---|
| before | ~12,123 tokens | Groq **413** → failover to Gemini → **429** → `result=None`, so `route_query()` returns its blind low-confidence RAG default |
| after | ~2,560 tokens | **`route=XAGG`** in **0.7 s** |

### What I did NOT change, and why

- **The deterministic overrides.** They are cheap, correct and auditable, and
  the brief asked for them to stay a fast path. Pinned by a test.
- **The local classification path.** This is the important one. The compact
  prompt was measured on local too, and on route labels it looked like a clean
  win: 55/96 → 61/96, gold unchanged at 29/32, a within-case control over
  router.txt's own 74 few-shot examples unchanged at 63/74 → 64/74, and **not
  one paraphrase regressing**. Then the five symptom questions were run
  end-to-end (§4) and it was a net regression:
  - **CR3** and **G6** fell from substantive XNETWORK answers to RAG's *"No
    sufficiently relevant documents were found"*;
  - **G6's English paraphrase reached DIRECT** and produced *"Welcome to the
    team! You're joining a dynamic and mission-driven environment…"* — a
    fabricated, ungrounded answer to a question about the real caseload, which
    is precisely what router.txt's own DIRECT rule exists to forbid.
  - The cause is identifiable rather than mysterious: the compact prompt drops
    router.txt's trailing `ACTIVE_CASE:` block, three of whose entries are gold
    questions **verbatim**. That block is exactly the gold-specific prompt debt
    this module was told not to add a fourth round of — and it is load-bearing.
    Removing gold-fitting has a real cost, and nothing here replaces it.
- **No fourth round of gold-specific regex patterns.** No pattern was added to
  any override list. The one new artefact, `router_compact.txt`, is derived
  from `router.txt`'s own content; a test asserts every router.txt example is
  still taught with the same route, so the two files cannot drift.
- **`cloud_max_tokens` stays 300.** With a ~2,560-token prompt the request is
  ~2,860, comfortably inside the cap, and 300 is the value already measured
  good for this reasoning model.
- **`secondary_methods`.** Preserved deliberately: the compact prompt keeps the
  `COMPOUND QUESTIONS` rule and all five compound examples, one for each of
  SQL/GRAPH/XGRAPH/XAGG. Pinned by a test, because the fast path never
  populates this field, so the prompt is the only place it can come from.

---

## 3. Unit tests

Eight new tests in `tests/test_router.py`. **Checked in both directions:**
with `src/pipeline/router.py` and `src/pipeline/json_extract.py` reverted via
`git stash`, **six fail**:

```
FAILED test_module92_local_path_still_sends_router_txt_unchanged
FAILED test_module92_cloud_escalation_uses_the_compact_prompt
FAILED test_module92_cloud_prompt_fits_the_provider_request_cap
FAILED test_module92_cloud_prompt_states_language_is_not_a_routing_signal
FAILED test_module92_cloud_prompt_preserves_secondary_methods
FAILED test_module92_cloud_prompt_does_not_drift_from_router_txt
```

The other two — `test_module92_deterministic_fast_path_is_untouched` and
`test_module92_cloud_system_prompt_defaults_to_no_change` — **pass before and
after by design**: they are invariance pins on things this module must not
change, and are reported as such rather than counted as change-detectors.

Suites run: `test_router` **150**, `test_orchestrator` **38**, `test_pipeline`
**46**, `test_harness_supervisor` **155**, `test_provider_failure_visibility`
**20** — **409 passed, 0 failed**. (A full `pytest -q` was deliberately not
run: it is known to empty the entity-embeddings collection.)

---

## 4. Live verification

**No backend was started.** The brief caps live backends at 2 machine-wide;
three were already listening (`127.0.0.1:8001`, `:8011`, `:8089`) and none ran
this branch. `evaluation/module92_inprocess_run.py` drives the same two stages
`main.py::chat_endpoint()` does — `route_query()`, then `run_cutover_query()`
through the Supervisor — against the same graph, vector store and model server.

Five questions × three languages, before and after, 40 runs. The "after" arm
here is the **compact-prompt-on-local** candidate, i.e. the arm that was
measured and then **rejected**; the shipped change cannot appear in this table
because it does not touch the local path at all.

| question | before (route → answer) | after (route → answer) | verdict |
|---|---|---|---|
| CR2 gold en | XAGG → 746 chars | XAGG → 746 chars | unchanged |
| CR2 para en | XAGG → 230 chars | XAGG → 230 chars | unchanged |
| CR2 para roman-ur | **XGRAPH** → 161 chars | **XGRAPH** → 161 chars | **Module 88's symptom, still open** |
| CR2 para ur | **XGRAPH** → 161 chars | **XGRAPH** → 161 chars | **Module 88's symptom, still open** |
| CR3 gold en | XNETWORK → 1610 chars | RAG → *abstains* | **regressed** |
| CR3 para ×3 | XNETWORK → 161/492/161 | RAG → *abstains* ×3 | regressed |
| G6 gold roman-ur | XNETWORK → 1494 chars, real district counts | RAG → *abstains* | **regressed** |
| G6 para en | XNETWORK → "no cross-case connections found" | **DIRECT** → 967 chars, fabricated | **regressed, worst case** |
| G6 para ur | XNETWORK → 161 chars | DIRECT → 240 chars | regressed |
| KB3 gold en | RAG → 1249 chars | RAG → 1177 chars | unchanged in kind |
| KB3 para en | XNETWORK → 644 chars | RAG → *abstains* | regressed |
| KB3 para roman-ur | RAG → 851 chars | RAG → 837 chars | unchanged in kind |
| KB9 gold roman-ur | RAG → *abstains* | RAG → 1518 chars | improved |
| KB9 para en/roman-ur | RAG → 511/284 chars | RAG → 490/284 chars | unchanged in kind |

**This table is why the local change did not ship.** Two clean regressions
(CR3, G6 gold), one fabrication (G6 para en), one improvement (KB9 gold).

**CR2's Roman-Urdu and Urdu paraphrases still route to XGRAPH** — Module 88's
symptom reproduces exactly, before and after, and **Module 92 does not close
it.** Neither do CR3's or G6's; KB3's and KB9's gold wordings were already
closed by Module 78, not by this module.

An honest note on CR3's "before" answer: at 1610 characters it *looks* like the
better outcome, but it answers a different question ("how many cases are
registered under the cybercrime act…"). Both arms fail CR3; one fails loudly
and one fails quietly.

---

## 5. Gold comparison

Judged on substance rather than wording, per the standing rule.

The shipped change moves **no** gold answer, because it does not touch the path
any gold question takes — every one of the 32 is answered either by a
deterministic override or by a local classification that is byte-identical
(§7). There is therefore no gold delta to compare.

For the **rejected** local arm, the gold-level comparison is §4's table: KB9's
Roman-Urdu gold moved from an abstention to a 1518-character answer (better),
while CR3's and G6's gold answers moved to abstentions (worse). Route accuracy
over gold was unchanged at 29/32 in both arms — the same three questions wrong
— which is precisely the metric that failed to predict any of this.

---

## 6. Non-gold paraphrase

This module *is* the paraphrase test; the full corpus result by language is the
table in §1.4. Restating the two rows that matter, over 96 paraphrases written
before any run and never adjusted:

| | en | roman-ur | ur | total |
|---|---|---|---|---|
| shipped router | 19/32 (59%) | **16/32 (50%)** | 20/32 (62%) | 55/96 (57%) |
| best measured (compact prompt, cloud model) | 22/32 (69%) | **22/32 (69%)** | 22/32 (69%) | 66/96 (69%) |

**One paraphrase changed the question's meaning and is reported rather than
rewritten**, as Module 88 did: G1's Roman-Urdu and Urdu paraphrases use
*"nigrani ka taqaza kare"* / *"نگرانی کا تقاضا کرے"* ("warrants monitoring").
`taqaza`/`تقاضا` is generic Urdu for "demands/requires", and it collides with
`rag.py::_NORM_SIGNAL_RE`'s legal-norm vocabulary. That is a real defect in the
gate (defect 99), not a flaw in the paraphrase — the English paraphrase of the
same question, with the same meaning, does not trigger it — so the text stands
as written.

---

## 7. Regression guard

**All 32 gold questions, and the whole router output dict, not just `route`.**

`route_query()` returns eight more load-bearing fields from the same LLM JSON,
and a change that left every route identical while quietly emptying
`secondary_methods` would sail through a route-only comparison and drop the
second half of every compound question. `evaluation/module92_route_dict_equality.py`
captured all 32 on the pre-change code (`git stash`) and on the shipped code:

```
  route              moves on 0/32
  case_scope         moves on 0/32
  target_entity      moves on 0/32
  output_format      moves on 0/32
  target_year        moves on 0/32
  station            moves on 0/32
  district           moves on 0/32
  secondary_methods  moves on 0/32

ALL LOAD-BEARING FIELDS EQUAL on all 32
```

Answers were not re-run for all 32, and the reason is stated rather than
assumed: with every routing field identical on every question, the inputs to
everything downstream are identical, and 64 more end-to-end runs would have
measured pipeline nondeterminism, not this change. The two questions whose
answers *could* have moved under the rejected arm (CR3, G6) were run
end-to-end anyway, in §4.

**A finding this control surfaced, which is not mine and must not be waved
through:** on unchanged `main`, **CR3, G1 and G6 no longer reach the `XAGG`
that Module 27 recorded on three passes** — all three now classify as
`XNETWORK`, reproducibly, offline and in-process. Module 27's "routes stable
32/32" no longer describes today's `main`, and the committed pass artefacts are
stale for these three. Filed as defect 100.

---

## 8. New defects found

Filed, not fixed.

**114 — the Gemini key pool is one throttled key and three invalid ones.**
`GEMINI_API_KEY` and `GEMINI_API_KEY_1` are the same key and return 429
(free tier: 20 `gemini-2.5-flash` requests/day); `GEMINI_API_KEY_2`, `_3` and
`_4` return `401 UNAUTHENTICATED`. `key_manager.rotate_key()` responds to a 429
by rotating into a dead key, turning a recoverable throttle into a hard
failure — observed as 95 consecutive 401s. This also affects
`evaluation/gold32_score.py`, whose judge resolves
`GEMINI_JUDGE_KEY or GEMINI_API_KEY` and whose `GEMINI_JUDGE_KEY` is unset, so
gold scoring is currently running on the exhausted key.

**115 — `_is_legal_kb_intent()` has a cross-lingual false positive.** In
`src/pipeline/harness/tools/rag.py`, the compound rule
`_NORM_SIGNAL_RE AND _OUR_DATA_SIGNAL_RE` fires on G1's Roman-Urdu and Urdu
paraphrases — `taqaza`/`تقاض` matches the norm signal and `hamare`/`ہمارے`
matches the our-data signal — routing a *creative-generation caseload* question
(expected XAGG) to RAG. The English paraphrase of the same question does not
fire, because the English norm vocabulary contains nothing as generic as
"taqaza". The Roman-Urdu and Urdu norm vocabularies are broader than the
English one, so the gate over-fires in exactly the languages it was widened
for. Evidence: `scratchpad`-reproducible via `_is_legal_kb_intent()` directly;
these are the only two false positives across all 128 corpus items.

**116 — the gold-32 route baseline is stale on `main`.** CR3, G1 and G6 are
recorded as `XAGG` on all three of Module 27's passes and reach `XNETWORK`
today, on unchanged code — confirmed offline via `route_query()` and
end-to-end in-process. router.txt's own `ACTIVE_CASE:` block explicitly teaches
CR3 → XNETWORK and G6's shape → XNETWORK, so the prompt and the recorded
baseline disagree with each other. Any module treating the pass artefacts as
current ground truth for these three will draw the wrong conclusion.

**117 — the local classifier cannot reproduce 11 of its own prompt's
examples.** Scored against `prompts/router.txt`'s own 74 few-shot pairs — which
are *in the prompt it is reading* — the local Qwen3-14B answers **63/74 (85%)**.
Misses include `"Who is the complainant and who is the accused in this case?"`
→ XGRAPH (documented GRAPH) and `"Is وقاص علی نیازی known to associate with
anyone else in this case?"` → DIRECT (documented GRAPH). This quantifies the
unreliability router.py's opening comment asserts, and bounds what any
prompt-only fix can achieve.

**118 — a router.txt few-shot example violates router.txt's own schema.** The
example `"Is this suspect a repeat offender — has he shown up elsewhere
before?"` carries `target_entity: "this suspect"`, while the schema in the same
file states target_entity must be a literal identifier and "never a descriptive
phrase like 'the accused' or 'the suspect in CASE-009'". The prompt is teaching
the model to do the thing it forbids. (Not copied into
`router_compact.txt`, which sets it to null.)

---

## What would actually close Module 92

Stated plainly, with the number attached, because the module stays open:

The only measured design that makes English, Roman-Urdu and Urdu equal is a
**capable cloud classifier on the miss path** — 69%/69%/69% against the shipped
59%/50%/62%. The compact prompt shipped here removes the *technical* blocker
(the 413), so what remains is a **quota** decision: Groq's `on_demand` tier
sustains ~2.7 router calls/minute, and Gemini's free tier is 20 requests/day on
three dead keys. A paid tier on either provider, or a self-hosted classifier
better than Qwen3-14B, turns this from blocked into a one-line change —
`escalate_to_cloud_on_failure` already exists, and now works.

Whoever picks this up should also take §4's lesson: **judge it on answers.**
This module's route-accuracy harness is committed and reusable, and it would
have told you to ship something that made the product worse.
