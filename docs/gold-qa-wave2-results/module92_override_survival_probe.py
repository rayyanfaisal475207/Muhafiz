# -*- coding: utf-8 -*-
"""
Module 92 — does `router.py`'s deterministic override survive a rewording?

Offline: imports `_deterministic_route_override()` directly, so it needs no
backend, no model server and no database. "LLM" in the output means no
override matched, and the route is then decided by the local Qwen3-14B
classifier that router.py's own module comment (line ~42) records as
unreliable on novel phrasings.

    PYTHONPATH=. python -X utf8 docs/gold-qa-wave2-results/module92_override_survival_probe.py
"""
import io
import json

from src.pipeline.router import _deterministic_route_override as ovr

GOLD_PATH = "evaluation/Gold_QA_Dataset_Final32_With_Answers.json"

# Same intent as gold, gold's own vocabulary deliberately not reused.
# Written before the probe was run and not adjusted afterwards.
PARAPHRASES = {
    "D1": ("What's the total number of FIRs on the books right now?",
           "Abhi register shuda kul FIRs ki tadaad kitni hai?"),
    "CR2": ("Do we have any repeat offender who already had a case years ago "
            "and now shows up again as a suspect in a fresh FIR?",
            "Koi aisa shakhs hai jiska pehle se koi case tha aur ab kisi naye "
            "FIR mein dobara mulzim bana ho?"),
    "CP1": ("Which districts recover weapons most often, as a share of their cases?",
            "Konsay zilon mein sab se ziyada asliha pakra jata hai?"),
    "M2": ("What does the workload look like station by station?",
           "Har police station par kitna kaam ka bojh hai?"),
    "CS4": ("How many accused have a prior record that our local data cannot match?",
            "Kitne aisay mulzim hain jinka purana record to hai magar yahan match nahi hota?"),
    "S2": ("Give me the count of people recorded as accused across all cases.",
           "Tamam cases mein mulzim ke tor par darj afraad ki tadaad batayein."),
    "CR4": ("Which weapons keep showing up again and again?",
            "Kaunsay hathiyar baar baar samne aate hain?"),
    "M1": ("Has the mix of crime we handle shifted compared with a couple of years ago?",
           "Do saal pehle ke muqable ab hamare cases ki noiyat mein kya farq aaya hai?"),
}


def route(query: str) -> str:
    decided = ovr(query) or {}
    return decided.get("route") or decided.get("retrieval_method") or "LLM"


def main() -> None:
    gold = {r["id"]: r["question"]
            for r in json.load(io.open(GOLD_PATH, encoding="utf-8"))}
    en_ok = ru_ok = total = 0
    print(f"{'Q':5}{'gold':>10}{'en-para':>10}{'ru-para':>10}")
    print("-" * 35)
    for qid, (para_en, para_ru) in PARAPHRASES.items():
        g, e, r = route(gold[qid]), route(para_en), route(para_ru)
        total += 1
        en_ok += (e == g)
        ru_ok += (r == g)
        print(f"{qid:5}{g:>10}{e:>10}{r:>10}")
    print()
    print(f"English paraphrase keeps gold's route:    {en_ok}/{total}")
    print(f"Roman-Urdu paraphrase keeps gold's route: {ru_ok}/{total}")


if __name__ == "__main__":
    main()
