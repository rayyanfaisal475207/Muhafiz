# Module 27 — pre-flight environment verification

**Run 2026-09-08 against `main` @ `687c87a`**, on the live stack, before the
final Gold-32 rerun.

**Why this file exists.** The 2026-09-08 post-fix evaluation's **first pass was
completely invalid** — not from any code defect, but because `SHARE/.env` was
never copied into place, so a stale `.env` carried 3 of 5 wrong Groq keys,
produced **1,410 rate-limit errors**, and made every KB question abstain. The
KB bucket read 0.000 and looked like a catastrophic regression in work that was
in fact fine.

An environment failure and a code failure are indistinguishable in the output.
So Module 27 must not start until the environment is *known* good, and the
evidence must be recorded rather than remembered. These are the checks
`HOW_TO_REPRODUCE_THIS_EVALUATION.md` §1.3 specifies, run and captured.

## Results — every check passed

| Check | Expected | Measured | |
|---|---|---|---|
| `cases` rows | 73 | **73** | ✅ |
| `Person` nodes | 430 | **430** | ✅ |
| accused `INVOLVED_IN` edges | 94 (**189 ⇒ duplicated**) | **94** | ✅ |
| `Incident.incident_datetime` | 64 | **64** | ✅ |
| Weapon `license_status` unlicensed | 30 | **30**, as `بغیر لائسنس` | ✅ |
| Mojibake markers (`╪`, `█`) in case text | 0 | **0** | ✅ |
| Chroma `muhafiz_kb` | 7,716 | **7,716** | ✅ |
| Chroma `muhafiz_community_reports` | 18 | **18** | ✅ |
| Chroma `muhafiz_entity_descriptions` | 568 | **568** | ✅ |

Two Weapon nodes carry an empty `license_status`; 30 of the remaining 32 are
unlicensed, which is the figure G5 asserts.

## What each check rules out

- **94 accused edges, not 189** — the graph has not been re-projected over
  itself. Duplicate edges would silently double every per-accused count, and
  the arrest-rate and relationship aggregates (Modules 32, 35) would report
  plausible but wrong figures.
- **Urdu renders as `بغیر لائسنس`, not `╪¿╪║...`** — the dump was not taken or
  restored through a PowerShell pipe. That corruption previously turned G5's
  answer into "0 of 32 unlicensed" instead of 30, and cost a full eval run to
  diagnose.
- **`incident_datetime` on 64 of 73** — Module 22's projection is present. The
  *previous* SHARE dump had **zero**, which would silently break M7, M5, M4 and
  the time-of-day aggregate with no error at all.
- **`muhafiz_entity_descriptions` at 568, not 0** — a full `pytest -q` run
  empties that collection, and Local Search then finds nothing while looking
  like a code bug.

## Still required before Module 27 runs

These are **not** covered above and must be confirmed at run time:

1. **`SHARE/.env` copied into place**, per `SETUP.md` step 1. This is the exact
   omission that invalidated the last pass.
2. **Quota errors stay near zero** *during* the run. If they climb into the
   hundreds, stop — the run is not measuring the code.

   **`grep -c "rate limit"` is NOT sufficient, and this file said it was.**
   Module 42 hit a transient Gemini **`429 RESOURCE_EXHAUSTED`** that the
   phrase "rate limit" does not match at all, so the prescribed check reported
   a clean run while a judge call had in fact failed. Both providers, and the
   generic 429, must be covered:

   ```bash
   grep -ciE "rate limit|RESOURCE_EXHAUSTED|quota|UNAVAILABLE|(^|[^0-9.,:])(429|503)([^0-9]|$)" backend.log
   ```

   **[Module 81] The numeric codes are boundary-guarded.** The bare form
   matched a **timestamp**: a line stamped `16:19:29,503` scored as a 503
   UNAVAILABLE, so every run certified clean with the old pattern was
   reading its own milliseconds as a provider failure. Ephemeral ports
   (`127.0.0.1:62503`) and latencies (`0.429s`) matched too.

   Groq says "rate limit"; Gemini says `RESOURCE_EXHAUSTED`. Checking only the
   first is how a quota failure gets published as a model failure.

   **`UNAVAILABLE|503` added by Module 54.** Module 50 hit
   `503 UNAVAILABLE ... 'This model is currently experiencing high demand.'`
   live — a transient *capacity* failure rather than a quota one, but with the
   same consequence (the call did not happen) and the same invisibility: the
   Module 46 pattern above returned 0 over it. The canonical form of this
   pattern lives in `evaluation/gold32_score.py`'s `LOG_GREP_PATTERN`.

3. **A failed cutover classification invalidates the run the same way.**
   Module 54 made it visible on the SSE stream and in
   `gold32_pipeline_outputs.json`; check both:

   ```bash
   grep -c "Cutover classification failed" backend.log
   python -c "import json;print(sum(1 for r in json.load(open('evaluation/gold32_pipeline_outputs.json',encoding='utf-8')) if r.get('cutover_classification_failed')))"
   ```

   A non-zero count means the harness was skipped and the legacy orchestrator
   re-routed those questions — a different sub-agent answered than the one
   being measured.
4. **Module 46 settled first.** The judge truncates answers at 900 characters,
   and the current build produces 1,000–2,500-character KB answers. Module 45
   proved with a positive control that a gold fact past that cut scores **0.0
   instead of 1.0**. It does not fire on the older committed run (only 2 of 32
   answers exceed the cap) but it is expected to fire on this one.
5. **Judge variance is real.** Module 45 measured the judge moving **0.3 on D1
   across five identical draws.** Do not read a sub-0.3 single-question
   movement as a code effect.
6. **Commit the artefacts.** `gold32_results.json` and
   `gold32_pipeline_outputs.json` on `main` are still the **2026-09-06** Module
   9 run (0.394 / 13-of-32), not the post-fix report's numbers — that is Module
   47. Module 27's rerun regenerates them, and committing them closes 47.

## Reproducing these checks

```bash
docker exec muhafiz-postgres psql -U postgres -d muhafiz -tAc "SELECT count(*) FROM cases;"
```

The graph checks run through AGE; the graph is named **`evidence_graph`** (not
`muhafiz_graph`), and each session needs `LOAD 'age'; SET search_path =
ag_catalog, public;` first.
