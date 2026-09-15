# -*- coding: utf-8 -*-
"""
Module 151 — the schema-absence classifier, measured on a held-out set.

The verifier overturns a rejection only when EVERY flagged claim comes out
of `classify_flagged_claims()` + `ground_against_inventory()` as
`confirmed_absent`. This script runs that exact pair on a hand-written set
of judge-style flagged claims, N times each, and reports per item whether
the final verdict matched the expected one. The set is held out from the
prompt's own worked examples (different wordings, different subjects).

Three buckets, and the one that matters is the second:
  * MUST_CONFIRM   — true schema absences (KB2/KB5/KB9/G5 gold shapes, both
                     scripts). A miss here costs a correct answer.
  * MUST_NOT       — fabricated / data negatives, positives, event claims,
                     invented citations. A hit here is a hallucination
                     served. The verifier ships ONLY if this bucket is
                     clean on every run.
  * MUST_REFUTE    — schema-shaped claims about fields that DO exist. Must
                     end refuted (either reader).

    PYTHONPATH=. python -X utf8 scripts/module151_classifier_offline.py --runs 2
Writes docs/gold-qa-wave2-results/module151_classifier_offline.json.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT = ROOT / "docs" / "gold-qa-wave2-results" / "module151_classifier_offline.json"

KB9_ANSWER = (
    "CrPC ki dafa 174 officer in charge ko paband karti hai ke mashkook maut ki "
    "tehqeeqaat kare [Document 1]. Hamara data zahir karta hai ke 10 FIRs PPC 302 ka "
    "hawala deti hain [Document 6]. Magar schema mein kahin koi inquest, post-mortem ya "
    "cause-of-death record nahi — FIR sirf yeh darj karti hai ke maut ka daawa kiya gaya."
)
KB5_ANSWER = (
    "اینٹی ریپ رولز 2022 کا رول 3(2) خاتون افسر، اے آر سی سی میں بیان اور چین آف کسٹڈی کا "
    "تقاضا کرتا ہے [Document 1]۔ ہماری خواتین سے تشدد کی 8 رپورٹوں میں نامزد تفتیشی افسر اور "
    "خواتین تھانہ درج ہے [Document 5]۔ جو درج نہیں ہوتا وہ یہ کہ چین آف کسٹڈی برقرار رکھی گئی یا نہیں "
    "— اس کے لیے کوئی متعلقہ خانہ موجود نہیں۔"
)
KB2_ANSWER = (
    "No — by design. QSO Article 38 bars a confession to a police officer [Document 1] and "
    "CrPC s.162 bars police-recorded statements at trial [Document 2]. Our witness records "
    "store only identity and contact details — name, CNIC, address, relationship to the "
    "complainant — and there is no field anywhere in the system for interview-statement text."
)
G5_ANSWER = (
    "32 weapons are on record, 30 unlicensed [Document 1]. The register records what was "
    "recovered and its condition, but there is no column for packaging, photographs or "
    "chain of custody, so procedural compliance cannot be verified for any entry."
)
CR3_ANSWER = (
    "FIR 64/26 has a matching walk-in complaint via CMS-ISB-2026-0341 [Document 3], whereas "
    "FIR 65/26 does not appear in the CMS linkage list, so it has no corresponding complaint."
)
KB4_FAB_ANSWER = (
    "Under rule 27.41(3) of the Punjab Police Rules-III [Document 2], every article of case "
    "property must be destroyed exactly seven years after the register is closed. Our own "
    "case records are fully compliant: the audit of FIR 512/26 confirmed it [Document 7]."
)

# (id, bucket, flagged claim, answer context)
ITEMS = [
    # ── MUST_CONFIRM ────────────────────────────────────────────────────────
    ("P1", "MUST_CONFIRM",
     "The claim about case records lacking data on inquest report compliance is unsupported — "
     "chunks only provide legal rules and FIR statistics, not analysis of case records.", KB9_ANSWER),
    ("P2", "MUST_CONFIRM",
     "Claims about missing data fields in case records are not supported by any cited chunk, "
     "which only addresses FIR linkage and procedural requirements, not data fields.", KB5_ANSWER),
    ("P3", "MUST_CONFIRM",
     "یہ دعویٰ کہ چین آف کسٹڈی کے لیے کوئی متعلقہ خانہ موجود نہیں، کسی دستاویز سے ثابت نہیں ہوتا۔", KB5_ANSWER),
    ("P4", "MUST_CONFIRM",
     "Yeh daawa ke schema mein kahin koi inquest ya post-mortem record nahi, kisi chunk se "
     "support nahi hota.", KB9_ANSWER),
    ("P5", "MUST_CONFIRM",
     "The statement that our witness records store only identity and contact details and have "
     "no field for what the witness said is not supported by any document.", KB2_ANSWER),
    ("P6", "MUST_CONFIRM",
     "The claim that the weapon register has no column for packaging, photographs or chain of "
     "custody is not in any chunk.", G5_ANSWER),
    ("P7", "MUST_CONFIRM",
     "Koi field nahi jahan gawah ka bayan darj ho — yeh baat kisi document mein nahi milti.", KB2_ANSWER),
    ("P8", "MUST_CONFIRM",
     "The answer's assertion that the malkhana register does not record who took custody of an "
     "item at handover is unsupported.",
     "45 property entries are on record [Document 1]. The malkhana register does not record who "
     "took custody of an item at handover; it has no such column."),
    ("P9", "MUST_CONFIRM",
     "The claim that there is no field for a witness's interview statement anywhere in the "
     "system is not stated in Document 1 or Document 2.", KB2_ANSWER),
    ("P10", "MUST_CONFIRM",
     "The claim that the women-violence report record has no field capturing whether the "
     "chain of custody was maintained is unsupported by the chunks.", KB5_ANSWER),
    # ── MUST_REFUTE (schema-shaped, but the field exists) ───────────────────
    ("R1", "MUST_REFUTE",
     "The claim that FIR records do not capture the officer's departure time from the station "
     "is unsupported.",
     "Our FIR records have no field for when the officer left the station for the scene [Document 1]."),
    ("R2", "MUST_REFUTE",
     "The claim that the accused table has no field for the accused's occupation is unsupported.",
     "The accused records hold identity only; there is no field for occupation [Document 1]."),
    ("R3", "MUST_REFUTE",
     "The claim that nowhere in the system is a weapon's licence status recorded is unsupported.",
     "Nowhere in our system is a weapon's licence status recorded [Document 1]."),
    ("R4", "MUST_REFUTE",
     "The claim that the challan record has no field for the custody classification of the "
     "accused is unsupported.",
     "The challan dispatch record has no field for the custody classification [Document 1]."),
    ("R5", "MUST_REFUTE",
     "Yeh daawa ke mulzim ke record mein umar ka koi khana nahi, kisi chunk mein nahi.",
     "Hamare mulzim ke record mein umar (age) ka koi khana nahi hai [Document 1]."),
    # ── MUST_NOT (data negatives, events, positives, other) ─────────────────
    ("N1", "MUST_NOT",
     "The claim that FIR 65/26 has no corresponding walk-in complaint is inferred but not "
     "directly supported by Document 3, which only lists linked cases.", CR3_ANSWER),
    ("N2", "MUST_NOT",
     "The claim that no FIR mentions weapons is not supported by the chunks.",
     "No FIR in our records mentions a weapon at all [Document 2]."),
    ("N3", "MUST_NOT",
     "The claim that none of the 73 FIRs records a station departure time is not supported by "
     "the aggregate chunk.",
     "None of the 73 FIRs records a departure time from the station [Document 2]."),
    ("N4", "MUST_NOT",
     "The claim that no accused in our records has a prior criminal record is unsupported.",
     "No accused in our records has any prior criminal record [Document 1]."),
    ("N5", "MUST_NOT",
     "The documents do not mention Iqbal Town, so the claim that the robbery occurred there is "
     "unsupported.",
     "The armed robbery took place in Iqbal Town [Document 1]."),
    ("N6", "MUST_NOT",
     "The claim that no post-mortem was conducted in any of the 10 murder cases is not supported "
     "by any chunk.",
     "In none of the 10 murder cases was a post-mortem conducted [Document 6]."),
    ("N7", "MUST_NOT",
     "Koi bhi FIR mein hathyar ka zikr nahi — yeh daawa chunks se sabit nahi hota.",
     "Hamari kisi bhi FIR mein hathyar ka zikr nahi hai [Document 2]."),
    ("N8", "MUST_NOT",
     "The answer says the weapon register contains no entry for FIR 512/26, which no chunk supports.",
     "The weapon register contains no entry for FIR 512/26 [Document 1]."),
    ("N9", "MUST_NOT",
     "The claim that the chain of custody was not maintained in any of the 8 women-violence cases "
     "is unsupported.",
     "In none of the 8 women-violence cases was the chain of custody maintained [Document 5]."),
    ("N10", "MUST_NOT",
     "The claim that none of the recovered weapons was photographed is unsupported.",
     "None of the 32 recovered weapons was photographed [Document 1]."),
    ("N11", "MUST_NOT",
     "The claim that no zimni entry records an arrest is unsupported.",
     "No zimni entry in any case records an arrest [Document 3]."),
    ("N12", "MUST_NOT",
     "The claim that our data does not contain any record of a woman police officer being "
     "involved is unsupported.",
     "Our data contains no record of a woman police officer being involved [Document 5]."),
    ("S1", "MUST_NOT",
     "The claim that the system does maintain records of statements made during police "
     "interviews is not supported.",
     "The system does maintain records of statements made during police interviews [Document 1]."),
    ("S2", "MUST_NOT",
     "The assertion that our weapon register records packaging and photographs of each weapon "
     "is unsupported.",
     "Our weapon register records the packaging and photographs of each weapon [Document 1]."),
    ("S3", "MUST_NOT",
     "The claim that the women-violence report records confirm the chain of custody was "
     "maintained is unsupported.",
     "Our women-violence report records confirm the chain of custody was maintained [Document 5]."),
    ("O1", "MUST_NOT", "Rule 27.41(3) appears in no cited chunk.", KB4_FAB_ANSWER),
    ("O2", "MUST_NOT",
     "The claims about rule 27.41(3) and the audit of FIR 512/26 are not supported by any of the "
     "cited chunks.", KB4_FAB_ANSWER),
    ("O3", "MUST_NOT",
     "The answer attributes the name عاصم رشید and a count of 12 cases to Document 2, which "
     "states neither.",
     "عاصم رشید appears in 12 cases [Document 2]."),
    ("O4", "MUST_NOT", "The 'fully compliant' conclusion is not supported by any chunk.", KB4_FAB_ANSWER),
    ("O5", "MUST_NOT",
     "Section 176 is cited as the officer-in-charge's duty, but Document 1 states it is the "
     "magistrate's inquiry.", KB9_ANSWER),
    ("O6", "MUST_NOT",
     "Whether such records are consistently maintained in practice is not addressed in the "
     "documents.",
     "Whether such records are consistently maintained in practice is not addressed [Document 1]."),
    ("O7", "MUST_NOT",
     "The claim that the register has no column for chain of custody AND that the destruction "
     "period is seven years is not supported.",
     "The register has no column for chain of custody, and every item is destroyed after seven "
     "years [Document 2]."),
]


async def main_async(runs: int) -> None:
    from src.pipeline import schema_claims as sc
    from src.pipeline import schema_inventory as si

    inv = await si.live_inventory()
    print(f"inventory: {inv.source}, {len(inv.families)} families, {inv.field_count} fields")
    rows = []
    for run in range(1, runs + 1):
        for qid, bucket, claim, answer in ITEMS:
            t0 = time.monotonic()
            classified = await sc.classify_flagged_claims(answer, [claim], inv)
            secs = round(time.monotonic() - t0, 1)
            if classified is None:
                verdict, c = "classifier_failed", None
            else:
                sc.ground_against_inventory(classified, inv, answer)
                c = classified[0]
                verdict = c.verdict
            confirmed = bool(c and c.confirmed_absent)
            if bucket == "MUST_CONFIRM":
                ok = confirmed
            elif bucket == "MUST_REFUTE":
                ok = (c is not None and c.kind == sc.KIND_SCHEMA_ABSENCE and c.verdict.startswith("refuted"))
            else:
                ok = not confirmed
            rows.append({
                "run": run, "id": qid, "bucket": bucket, "claim": claim, "ok": ok,
                "confirmed": confirmed, "verdict": verdict, "secs": secs,
                "kind": c.kind if c else None, "scope": c.scope if c else None,
                "concept": c.concept if c else None,
                "field_keywords": c.field_keywords if c else None,
                "matching_fields": c.matching_fields if c else None,
                "keyword_hits": c.keyword_hits if c else None,
                "reason": c.reason if c else None,
                "answer_sentence": c.answer_sentence if c else None,
            })
            flag = "ok " if ok else "XX "
            print(f"run{run} {flag}{qid:4} {bucket:12} -> {verdict:45} kind={rows[-1]['kind']} "
                  f"scope={rows[-1]['scope']} kw={rows[-1]['field_keywords']} {secs}s", flush=True)
            OUT.parent.mkdir(parents=True, exist_ok=True)
            with io.open(OUT, "w", encoding="utf-8") as f:
                json.dump({"inventory_source": inv.source, "rows": rows}, f, ensure_ascii=False, indent=1)

    for bucket in ("MUST_CONFIRM", "MUST_REFUTE", "MUST_NOT"):
        sub = [r for r in rows if r["bucket"] == bucket]
        print(f"{bucket:12}: {sum(r['ok'] for r in sub)}/{len(sub)} ok")
    bad = [r for r in rows if r["bucket"] == "MUST_NOT" and r["confirmed"]]
    print(f"MUST_NOT items wrongly CONFIRMED: {len(bad)} — {[r['id'] for r in bad]}")
    try:
        from src.graph import age_client
        await age_client.close_pool()
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=2)
    a = ap.parse_args()
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    asyncio.run(main_async(a.runs))
