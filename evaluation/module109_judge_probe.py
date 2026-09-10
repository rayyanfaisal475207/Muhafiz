"""Module 109 — the judge comparison harness, committed so the comparison is
reproducible.

Module 87 committed its RESULTS (`module87_*.json`) but not the script that
produced them, and it did not commit the INPUT TEXT of its three held-out
controls — only prose describing them in `MODULE87_RESULT.md` §6. Reconstructing
them was the first job of Module 109; the reconstruction is validated (it
reproduces Module 87's numbers on Module 87's own judge, exactly: A 1.0/1.0/1.0,
B 0.2 x3, C 0.2 x3), but it should not have had to be reconstructed at all. The
controls now live in this file, in code.

Nothing here touches the platform: every run is a re-scoring of the answers
Module 27 already captured (`gold32_pass{1,2,3}_outputs.json`), so the system
under test is frozen and the only variable is the judge. **No backend, no
Docker, no model server.**

    PYTHONPATH=. .venv/Scripts/python.exe -X utf8 \n        evaluation/module109_judge_probe.py --provider groq \n        --model openai/gpt-oss-20b --mode controls --draws 3 --out controls.json

Modes: `m7` (the headline question), `probe` (Module 87's nine probe questions,
five draws each — the only protocol that isolates the judge from the pipeline's
own non-determinism), `controls` (the three held-out cases), `kb9` (the
regression the polarity rule caused, checked on every candidate), `rescore`
(all 32 questions x 3 passes = 96 calls, comparable row-for-row with
`module87_rescore_3pass.json`).
"""
from __future__ import annotations
import argparse, json, os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
# Repo-root `.env` by default, matching `gold32_score.py`. `GOLD32_ENV_FILE`
# exists because this project runs from git worktrees, which do not carry the
# untracked `.env` — without the override the judge is silently built with
# api_key=None and every question fails identically, which looks like a model
# outage rather than a missing file.
try:
    from dotenv import load_dotenv
    load_dotenv(os.environ.get("GOLD32_ENV_FILE") or os.path.join(ROOT, ".env"))
except Exception:  # noqa: BLE001
    pass

import evaluation.gold32_score as gs

PROBE_IDS = ["CP1", "CP6", "CR2", "D1", "G1", "KB2", "KB6", "M2", "M7"]

M7_Q = ("Kya log 2026 mein waqiaat ki police ko itni hi jaldi ittila de rahe "
        "hain jitni 2024 mein dete the?")
M7_EXP = ("Haan, bohat zyada farq se. 2024 ka ausat 15.0 minute hai — "
          "taqreeban foran, ek jari dakaiti jo saath hi report hui. 2026 ka "
          "ausat 1401.3 minute (~23.4 ghante) hai — kaafi sust, jo fraud/cyber "
          "cases ki taraf tabdeeli se mutabiq hai jinhein mutasireen baad mein "
          "maloom karte hain.")

# Controls, reconstructed from MODULE87_RESULT.md §6, which describes each one
# but does not commit its input text (see MODULE109_RESULT.md, new defect).
# The Gemini baseline is RE-MEASURED on these same reconstructed texts so the
# comparison is like-for-like rather than against Module 87's stored numbers.
CONTROLS = [
    dict(case="A_heldout_positive", expect="HIGH",
         q="Kya har FIR mein koi naamzad tafteeshi afsar mojood hai?",
         exp=("Nahin — sab mein nahi. 11 FIRs mein naamzad tafteeshi afsar ki "
              "jagah sirf ek placeholder darj hai: \"(Naamzad ASI)\" 8 par aur "
              "\"(Naamzad SI)\" 3 par. Iske muqable mein sab se masroof naamzad "
              "afsraan (Faisal aur Tariq) ko 4-4 FIRs assign hain."),
         act=("Yes, there is a gap: 11 FIRs have no real investigating officer "
              "named. Eight of them record \"(Naamzad ASI)\" and three record "
              "\"(Naamzad SI)\" as a placeholder in the named-officer field. "
              "The busiest named officers, Faisal and Tariq, hold 4 FIRs each.")),
    dict(case="B_reversed_fact", expect="LOW", q=M7_Q, exp=M7_EXP,
         act=("Yes, people are reporting incidents to the police just as "
              "quickly — in fact faster. The mean time from incident to report "
              "was 1401.3 minutes (~23.4 hours) across 13 FIRs in 2024, "
              "whereas it was 15.0 minutes across 51 FIRs in 2026 "
              "[Document 1].")),
    dict(case="C_wrong_magnitude", expect="LOW", q=M7_Q, exp=M7_EXP,
         act=("No, people are not reporting incidents to the police as quickly "
              "in 2026 as they did in 2024. The mean time from incident to "
              "report was 15.0 minutes across 13 FIRs in 2024, whereas it was "
              "18.0 minutes across 51 FIRs in 2026 [Document 1].")),
]


def load_pass(n):
    p = os.path.join(ROOT, "evaluation", "gold32_pass%d_outputs.json" % n)
    return {r["id"]: r for r in json.load(open(p, encoding="utf-8"))}


def score_one(judge, q, exp, act, metric_name="FactualCorrectness"):
    from deepeval.test_case import LLMTestCase
    m = gs.build_metrics(judge)[metric_name]
    tc = LLMTestCase(input=q, actual_output=act, expected_output=exp,
                     retrieval_context=[exp])
    t0 = time.time()
    score, reason = gs.measure(m, tc)
    return score, (reason or "")[:300], round(time.time() - t0, 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--mode", required=True,
                    choices=["probe", "controls", "kb9", "rescore", "m7"])
    ap.add_argument("--draws", type=int, default=5)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    os.environ["GOLD32_JUDGE_PROVIDER"] = a.provider
    os.environ["GOLD32_JUDGE_MODEL"] = a.model
    import importlib
    importlib.reload(gs)
    judge = gs._judge()

    rows = []
    def emit(**kw):
        rows.append(kw)
        print(json.dumps(kw, ensure_ascii=False)[:220], flush=True)
        json.dump(rows, open(a.out, "w", encoding="utf-8"),
                  indent=1, ensure_ascii=False)

    if a.mode in ("probe", "m7", "kb9"):
        ids = (PROBE_IDS if a.mode == "probe"
               else ["M7"] if a.mode == "m7" else ["KB9"])
        p1 = load_pass(1)
        for qid in ids:
            r = p1[qid]
            for d in range(a.draws):
                s, why, el = score_one(judge, r["question"],
                                       r["expected_answer"], r["actual_answer"])
                emit(id=qid, **{"pass": 1}, draw=d, metric="FactualCorrectness",
                     model=a.model, provider=a.provider, prompt="new",
                     score=s, elapsed_s=el, reason=why)
    elif a.mode == "controls":
        for c in CONTROLS:
            for d in range(a.draws):
                s, why, el = score_one(judge, c["q"], c["exp"], c["act"])
                emit(case=c["case"], draw=d, expect=c["expect"],
                     model=a.model, provider=a.provider, prompt="new",
                     score=s, elapsed_s=el, reason=why)
    elif a.mode == "rescore":
        for pn in (1, 2, 3):
            po = load_pass(pn)
            for qid, r in po.items():
                if gs.no_answer_captured(r):
                    emit(id=qid, **{"pass": pn}, draw=0,
                         metric="FactualCorrectness", model=a.model,
                         provider=a.provider, prompt="new", score=None,
                         elapsed_s=0.0, reason=gs.no_answer_reason(r))
                    continue
                s, why, el = score_one(judge, r["question"],
                                       r["expected_answer"], r["actual_answer"])
                emit(id=qid, **{"pass": pn}, draw=0,
                     metric="FactualCorrectness", model=a.model,
                     provider=a.provider, prompt="new", score=s,
                     elapsed_s=el, reason=why)
    print("DONE", a.out, len(rows))


if __name__ == "__main__":
    main()
