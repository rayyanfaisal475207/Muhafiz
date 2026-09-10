# Module 37 — the orphaned rows are real, the deletion is right, and it did not fix KB2

**Branch** `fix/orphaned-bm25-rows` · **worktree** `D:/Rapids AI/muhafiz-m37` · branched from `origin/main` @ `b66ce68` (Module 64 / PR #77 included).

**Headline, and it contradicts the filed diagnosis.** The mechanical claim is
confirmed exactly as filed: 2,246 `chunk_fulltext` rows pointed at Chroma ids
that no longer existed, they reached the evaluator on **all eight** RAG gold
questions, and deleting them removed every one of them from every window
(**KB2 3 of 5 chunks → 0 of 5**, including both ids Module 82 named by hand).

**The gold claim built on top of that is wrong.** Module 82 predicted that
removing the orphans would stop KB2 concluding *"it is not a data gap; the
system does maintain such records"*. Measured live, 4 runs of 4 post-deletion,
the answer does not stop saying it — **it says it more plainly than before**:

> "Therefore, it is not a data gap; the system does maintain records of
> statements made during police interviews."

Before the deletion, that sentence was *blocked*: the grounding verifier
rejected the answer 4 times of 4 in this module's dedicated KB2 arm, precisely
because it was built on empty chunks. Removing the orphans replaced them with
**real CrPC chunks about the duty to write statements down** — better
retrieval, and the model reasons straight from them to the opposite of gold.
The orphans were **masking** KB2's failure, not causing it.

**The deletion still ships.** Serving citation markers that resolve to nothing
is wrong regardless of what it scores, both stores are now exactly in sync at
**7,716 = 7,716** in both directions, and one gold question (**KB5**) genuinely
recovered. But this is a **cleanup, not a fix**, on two counts: KB2's claim
does not survive measurement, and **nothing in the code prevents the rows
coming back** (§5, new defects 139 and 140).

---

## 1. Root cause

### 1.1 What the orphans were

Derived live against both stores, not from a hardcoded list:

| | |
|---|---|
| `chunk_fulltext` rows before | 9,962 |
| Chroma `muhafiz_kb` ids | 7,716 |
| **orphans — in BM25, absent from Chroma** | **2,246** |
| — one superseded CrPC ingestion, `doc_id 0519abd8` | 2,243 |
| — leftover test fixtures (`e2e_test_doc.txt`, `kbfix_test_doc.txt`, `kbfix_test_doc2.txt`) | 3 |

The 2,243 are a single stale ingestion of the same PDF that is currently served
as `f9908363`. Raw ids: `module37_orphan_ids.json`.

### 1.2 Why they existed, and whether they recur

`chunk_fulltext` is maintained **forward only**. `fulltext_index.maintain()` is
called per chunk from `vector_store.upsert_documents()`; nothing removes a row
when the corresponding Chroma id goes away. Two distinct paths produce orphans,
and **both are still open** — filed as defects **139** and **140** in §8:

* **`ChromaVectorStore.drop_and_recreate()`** (`src/retrieval/vector_store.py`)
  deletes and recreates the collection and does not touch `chunk_fulltext` at
  all. Reached by `reset_collection()` and by
  `scripts/reset_evidence_state.py`, whose `_reset_postgres()` enumerates the
  tables it clears and **does not list `chunk_fulltext`**. After either, *every*
  row in the BM25 index is an orphan.
* **Re-ingestion of a changed source.** `Document._generate_id()` hashes
  `self.text[:200]`, so any change to extraction or chunking yields a different
  `doc_id`; the new chunks are inserted **alongside** the old ones rather than
  over them. This is exactly what produced `0519abd8` → `f9908363`.

One near-miss worth naming, because it looks like the missing cleanup and is
not: `upsert_documents()` *does* call `fulltext_index.delete_by_ids()`, but only
inside the `except Exception:` branch that compensates a failed Postgres write
by rolling back the chunks **that same call just inserted**. It can never retire
a superseded `doc_id`. Pinned by a test so the distinction is not lost again.

The only path that stays in sync is `src/api/admin.py::delete_kb_document`,
which calls `fulltext_index.delete_by_source()`.

**So: this deletion is a cleanup that must be repeated** after any Chroma reset
or any re-ingestion of an already-ingested source, until 139/140 are closed.

### 1.3 Blast radius, established structurally

At query time BM25 is read from exactly two places —
`src/pipeline/harness/tools/rag.py` and `src/pipeline/orchestrator.py`, both the
RAG path. No other route consults `chunk_fulltext`. That bounds the change to
the RAG questions and is what makes the all-32 control in §7 interpretable.

---

## 2. The change

`scripts/cleanup_orphaned_fulltext_rows.py` — a script, not a migration (see
§2.1). Properties, each one exercised by a test in §3:

* **Derives orphans live from both stores every run.** Never a hardcoded id
  list: the orphan set depends on which ingestion the store currently holds.
* **Reversible.** Every column except the derived `tsv` is written to
  `module37_orphan_backup.jsonl.gz` **before a single row is deleted**, the
  backup is re-read and every id checked into it, and only then does the delete
  run. `--restore` re-inserts them, rebuilding `tsv` with the same
  `to_tsvector('simple', <tokenized>)` expression `fulltext_index.maintain()`
  uses, so a restored row behaves identically to the deleted one.
* **Union-merging backup.** A later cleanup with a different orphan set cannot
  truncate an earlier one's rows out of the file.
* **Idempotent.** A second run finds nothing and exits 0 — demonstrated in §4.
* **Guarded.** Refuses to act if Chroma returns fewer than 1,000 ids. This is
  not theoretical: `CHROMA_PERSIST_DIR` is **relative** in `.env`
  (`./data/chroma_db`), so running the script from a git worktree opens a new,
  empty store, at which point every row in the index looks orphaned. Without
  the guard the script would back up and delete the entire BM25 index.

Backup verified independently of the script: **2,246 rows, all 9 columns**
(`chunk_id, doc_id, source, project_id, case_id, is_global, metadata, text,
updated_at`), matching the deleted set exactly.

### 2.1 Migration convention — checked, and deliberately not used

Both conventions exist in this repo: alembic (`alembic.ini`, 8 revisions, CI
runs `alembic upgrade head`) **and** hand-numbered SQL in `migrations/`
(013–032). The `alembic_version` table exists and reads `ae3e106053f8`, which is
the alembic head — so alembic is current, but it only covers the ORM schema.
Everything in `migrations/0NN_*.sql` sits outside it, **including
`022_chunk_fulltext_index.sql`, which created this very table**. So the
convention for `chunk_fulltext` is numbered SQL, not alembic.

Neither is the right vehicle here. A migration runs against Postgres alone; the
orphan set is defined by *which ids Chroma currently holds*, which no SQL
migration can know, and which differs per deployment. A migration that
hardcoded today's 2,246 ids would be wrong on every other machine and wrong on
this one after the next ingest. It ships as a script, and §6 documents running
it as a post-restore step.

---

## 3. Unit tests

`tests/test_module37_orphan_cleanup.py` — **9 tests, all green**, no Postgres
and no Chroma (temp files and an injected fake store).

```
PYTHONPATH=. python -m pytest tests/test_module37_orphan_cleanup.py -p no:randomly
9 passed
```

Four cover the backup that makes the deletion reversible (every column round
trips, gzip+UTF-8 including Urdu text, union-merge across two cleanups,
rewrite-replaces-rather-than-duplicates). Two cover the safety guard. Three are
**findings, not guards**: they assert the *current, defective* behaviour behind
139 and 140, so that closing either defect fails here and points whoever closes
it at this file.

---

## 4. Live verification

Backend on `:8037`, local Qwen3-14B throughout. **`generation` recorded on every
row** (Module 101 §1.4): **all 12 KB2 runs and all KB regression runs report
`local`** — zero silent Groq fallbacks, so no run is a cloud model citing
differently. 0 cutover fallbacks, 0 quota lines.

The `before` arm is **not re-runnable** — the deletion was applied to the shared
live database before this module resumed — so every "before" number below comes
from the artefacts captured pre-deletion (`module37_kb2.json`,
`module37_gold32.json`), never from a post-deletion run presented as a baseline.
Where a before measurement was not captured, it is marked as such.

### 4.1 The deletion, and its idempotency

```
chunk_fulltext rows : 7716
chroma muhafiz_kb   : 7716
orphans (BM25 \ Chroma) : 0
reverse (Chroma \ BM25) : 0
nothing to do.
```

9,962 − 2,246 = 7,716, and Postgres confirms 7,716. The second run is a no-op:
**idempotent**.

**Incidental finding for Module 99:** the reverse discrepancy — 790 Chroma ids
absent from BM25, the half Module 99 was opened for — now measures **0**. The
two stores are exactly in sync in both directions. Module 99 should re-measure
before doing any work; on this store there is currently nothing to fix.

### 4.2 KB2 — the claim under test

The window is deterministic in both arms (identical chunk ids on every run):

| Arm | Runs | Window | Orphans in window | Module 82's two ids | `status` |
|---|---|---|---|---|---|
| before | 4 | `f9908363_c675`, **`0519abd8_c590`**, `f9908363_c1010`, **`0519abd8_c601`**, **`0519abd8_c602`** | **3 of 5** | present | `error` 4/4 |
| after | 4 | `f9908363_c675`, `f9908363_c1010`, `68bb5d0d_c1695`, `68bb5d0d_c1694`, `f9908363_c673` | **0 of 5** | **gone** | `done` 4/4 |

**The retrieval half of the claim is confirmed.** Both `…_c590` and `…_c601` are
gone from the window, along with a third orphan (`…_c602`) Module 82 did not
record. Three dead chunks were replaced by three live ones.

**The answer half of the claim is refuted.** All 4 after-runs return a
byte-identical 937-character answer ending:

> "Therefore, it is not a data gap; the system does maintain records of
> statements made during police interviews."

This is the sentence the deletion was supposed to eliminate. What changed is
that it is now *served* rather than blocked — before, the grounding verifier
refused the answer 4 of 4.

**Why.** The replacement chunks (`68bb5d0d_c1694/c1695`, Punjab Police Rules)
are about the duty to reduce statements to writing. The model reads them
correctly and concludes the records exist. Gold's actual point is a fact about
**our product schema** — *"there is no field anywhere in the system for
confession or interview-statement text"* — which is **not in the statute corpus
at all**. No amount of BM25 hygiene can retrieve it. KB2 is unanswerable from
this corpus by retrieval alone; the orphans were hiding that behind a verifier
rejection.

---

## 5. Gold comparison

All eight RAG gold questions, **3 runs per arm**, `route=RAG` throughout,
`generation=local` on all 48 rows, **0 cutover fallbacks, 0 quota lines**.
"Anchor" is gold's own load-bearing provision appearing in the served answer.

| Q | orphans in window | answered | gold's anchor | anchor tested |
|---|---|---|---|---|
| **KB1** | 0–1 → **0** | 3/3 → 3/3 | **3/3 → 3/3** | CrPC s.154 |
| **KB2** | 3 → **0** | 2/3 → 3/3 | 0/3 → 0/3 | QSO Art. 38 / CrPC s.162 |
| **KB3** | 2 → **0** | 3/3 → 3/3 | 0/3 → 0/3 | Police Order Art. 18 |
| **KB4** | 1 → **0** | 3/3 → 3/3 | 0/3 → 0/3 | PPR rule 27.16 |
| **KB5** | 2 → **0** | **0/3 → 3/3** | **0/3 → 3/3** | Anti-Rape Rules 3(2) / ARCC |
| **KB6** | 2 → **0** | 3/3 → 3/3 | **3/3 → 3/3** | firearm packaged unloaded |
| **KB8** | 3 → **0** | 3/3 → 3/3 | 0/3 → 0/3 | CrPC s.173 |
| **KB9** | 2–3 → **0** | 2/3 → 3/3 | **2/3 → 0/3** | CrPC s.174 |

**Orphans are gone from every window on every run** — the mechanical claim, on
all eight questions, not just KB2.

**Scoreboard, stated plainly.** Answered **19/24 → 24/24**. Gold's anchor
**8/24 → 9/24**. The deletion buys a large gain in *answering rate* — five
grounding-verifier refusals disappear — and is close to **neutral on
correctness**: one question gains its anchor, one loses it, and one starts
asserting the opposite of gold with confidence.

### 5.1 Checked explicitly and by name

* **KB1** (fixed by Module 89) — **safe.** CrPC s.154 present 3/3 in both arms;
  the data half still composes. Nothing lost.
* **KB4** — **unchanged, and still wrong.** Rule 27.16 is reached 0/3 in *both*
  arms; the answers cite rules 22.16/22.18 instead. Pre-existing, neither caused
  nor cured here.
* **KB5** — **the one real win.** Before: the grounding verifier refused all 3
  runs. After: answered 3/3, and the Anti-Rape Rules 3(2)/ARCC anchor appears
  3/3. Two orphans left the window and real Anti-Rape Act chunks took the slots.
* **KB6** (fixed by Module 64) — **safe.** The unloaded/safety-on packaging
  anchor holds 3/3 in both arms; the answers are substantively equivalent.
* **KB8** — **unchanged, and still wrong.** Both arms answer on CrPC s.172 (the
  case diary) where gold wants s.173's 14-day interim report. Pre-existing.
* **KB9** (fixed by Modules 78 and 101) — **regressed.** Anchor s.174
  **2/3 → 0/3**. All three after-runs answer on **s.176** — the *magistrate's*
  inquiry into a custodial death — instead of s.174's officer-in-charge duty. It
  now answers 3/3 where it previously refused once, so it is no worse on
  `status`; it is worse on substance. Mechanism in §5.2.

### 5.2 Why it cuts both ways — the orphans were mostly duplicates

**1,871 of the 2,246 orphans (83%) carried text byte-identical to a chunk still
live in Chroma.** The stale CrPC ingestion was largely a second copy of the same
statute. BM25 ranked both copies, so a single passage could occupy **two** window
slots — one of them dead.

KB9 shows this exactly. Its before-window held **both** `…_0519abd8_c35` and
`…_f9908363_c35`, verified here as byte-identical text, one live and one dead.
After the deletion those slots went to `68bb5d0d_c1978` and `68bb5d0d_c925`
(Punjab Police Rules, custodial-death inquiry), and that is what pulled the
answer from s.174 to s.176.

So the deletion does not simply remove noise: it **frees window slots and admits
more diverse chunks**. That rescued KB5, and it displaced KB9's and KB2's correct
provisions. The remaining 375 orphans had no byte-identical live twin, which is
consistent with the re-ingestion having re-cut chunk boundaries rather than with
text having been lost.

### 5.3 KB2 — the filed claim, refuted

Across **7 pre-deletion runs** (4 in `module37_kb2.json`, 3 in the all-32 arm)
the window is identical every time and carries 3 orphans every time; `status` is
`error` on 5 and `done` on 2. The two `done` runs hedge — *"This does not
necessarily indicate a data gap but rather a legal provision…"* — and **never**
claim the system holds interview records.

Across **7 post-deletion runs** the window is identical, carries 0 orphans, and
the answer is `done` 7/7 and asserts the opposite of gold on every one.

Gold's substance is a fact about **our schema** — *"there is no field anywhere in
the system for confession or interview-statement text"*. That sentence is not in
the statute corpus, so no BM25 change can retrieve it. Module 82 read the
orphaned lead citation as the *cause*; measured, it was a **mask**. Filed as
defect 141.

---

## 6. Reproducing this from the SHARE dump

The backup is committed, so a restored deployment can reach either state:

```bash
# from the DEPLOYMENT ROOT — CHROMA_PERSIST_DIR is relative in .env
PYTHONPATH=. python scripts/cleanup_orphaned_fulltext_rows.py            # report only
PYTHONPATH=. python scripts/cleanup_orphaned_fulltext_rows.py --apply    # back up, then delete
PYTHONPATH=. python scripts/cleanup_orphaned_fulltext_rows.py --restore  # put them back
```

Run from the deployment root, **not** from a worktree — a worktree opens an
empty Chroma store and the guard will (correctly) refuse. Re-run `--apply` after
any Chroma reset or any re-ingestion of an already-ingested source, until
defects 139 and 140 are closed.

---

## 7. Regression guard

### 7.1 Routes — 32 of 32 unchanged

`last_dispatch_route` before vs after, all 32 gold questions: **0 changes.**

The three questions whose dispatch value differs from
`evaluation/gold32_route_baseline.json` (CR3, G1, G6 — `XAGG` here vs `XNETWORK`
there) differ **identically in both arms**. That is Module 116's finding, not a
regression: the baseline records the top-level `route_query()` result, while
these artefacts record the last `supervisor:dispatch` SSE, which for a
Meta-Analysis question is a decomposed sub-query's route. Two different
quantities; the one measured here did not move.

`route_query()` performs no retrieval at all, so it is structurally incapable of
noticing this change — the route baseline could not have moved.

### 7.2 The 24 non-RAG questions — bounded structurally, not measured

At query time `chunk_fulltext` is read from exactly two call sites, both on the
RAG path (§1.3). The 24 `XAGG` questions never enter it, and none of them logged
a retrieval window in either arm.

**Stated honestly:** those 24 have **n=1 per arm**, so where their answers differ
(CR3, M1, M4, M5, G1, G6) I **cannot** separate a regression from ordinary
generation nondeterminism — Module 83 documented G6 collapsing on 12 of 16
baseline runs, so divergence there is expected. The claim that they are
unaffected rests on the **structural** argument above, not on those runs. If a
stronger guarantee is wanted, those 24 need a multi-run arm; this module did not
buy one.

### 7.3 Unit tests

150 tests across `test_fulltext_index.py`, `test_chroma_vector_store.py`,
`test_kb_statute_retrieval.py` and `test_module37_orphan_cleanup.py` — all green.
No file under `src/` is modified by this module; the diff is one script, one test
file, four artefacts and this document.

---

## 8. New defects found

**Numbers re-checked against the tracker immediately before commit.**

**139 — a Chroma reset leaves the entire BM25 index orphaned.**
`ChromaVectorStore.drop_and_recreate()` empties the collection and never touches
`chunk_fulltext`; `scripts/reset_evidence_state.py::_reset_postgres()` enumerates
the tables it clears and `chunk_fulltext` is not among them. After either, **every
row** in the BM25 index points at an id that no longer exists — the state this
module just spent 2,246 rows cleaning up, re-created wholesale. Pinned by
`test_module37_defect_139_resetting_chroma_does_not_clear_chunk_fulltext`.

**140 — re-ingesting a changed source never retires the previous ingestion's
rows.** `Document._generate_id()` hashes `self.text[:200]`, so any change to
extraction or chunking yields a new `doc_id` and the new chunks are inserted
*alongside* the old ones. This is the direct cause of the 2,243 `…_0519abd8_`
rows. `upsert_documents()`'s `fulltext_index.delete_by_ids()` call is **not** the
missing cleanup — it is a rollback compensator inside the `except` branch, and it
only ever names ids that same call just inserted. Pinned by
`test_module37_defect_140_reingest_never_deletes_the_previous_ingestions_rows`.

**Together, 139 and 140 are why this module is a cleanup and not a fix.** The
script must be re-run after any Chroma reset or any re-ingestion of an
already-ingested source until they close.

**141 — KB2's gold rests on a product-schema fact that is not in the corpus, and
removing the orphans made the answer worse.** Gold's substance is *"there is no
field anywhere in the system for confession or interview-statement text"*. No
statute chunk says that, so retrieval cannot reach it. Post-deletion KB2 asserts
the opposite of gold — *"it is not a data gap; the system does maintain
records"* — on **7 of 7 runs**, where pre-deletion the grounding verifier blocked
the answer on 5 of 7. **Module 82's diagnosis is corrected: the orphaned lead
citation was masking this, not causing it.** KB2 needs a data-half plan that can
state the schema absence (the Module 39 / 77 / 89 mechanism), not better BM25.

**142 — KB9 answers on CrPC s.176 instead of s.174 once the duplicate CrPC chunk
leaves its window.** Anchor **2/3 → 0/3**. The orphan `…_0519abd8_c35` was a
byte-identical duplicate of the live `…_f9908363_c35`; removing it freed window
slots that Punjab Police Rules custodial-death chunks took, moving the answer
from the officer-in-charge duty (s.174, gold) to the magistrate's inquiry
(s.176). Touches Modules 78 and 101's question. The general form deserves its own
look: **83% of the orphans were duplicate text, so this deletion changes window
*composition* on every RAG question**, and KB9 is where that landed badly.

**Not a defect — an update for Module 99.** Its 790 Chroma-ids-absent-from-BM25
now measure **0**; both stores are exactly in sync at 7,716 in both directions.
Module 99 should re-measure before doing any work.
