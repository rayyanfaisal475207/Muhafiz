"""All-32 deterministic controls for Module 79. Run once with cwd/PYTHONPATH
on the pre-module tree and once on this tree; diff the two files.

Per gold question: the Meta-Analysis decomposition plan it matches (and
that plan's dispatch list), the RAG data-half plan it matches (and whether
that plan carries a second aggregate), and the router's deterministic
route override. None of these needs a model or the graph, so the equality
is exact.
"""
import json, os, sys
sys.path.insert(0, os.getcwd())
from src.pipeline.harness.agents.meta_analysis import _match_decomposition_plan
from src.pipeline.harness.tools.rag import _match_kb_data_half_plan
from src.pipeline import router

OUT = sys.argv[1]
GOLD = os.environ.get("GOLD", "evaluation/Gold_QA_Dataset_Final32_With_Answers.json")
out = {}
for it in json.load(open(GOLD, encoding="utf-8")):
    q = it["question"]
    mp = _match_decomposition_plan(q)
    kp = _match_kb_data_half_plan(q)
    ro = router._deterministic_route_override(q, case_id=None)
    out[it["id"]] = {
        "meta_plan": mp.name if mp else None,
        "meta_dispatch": [*(mp.sub_queries if mp else ()), *[c.label for c in getattr(mp, "chained", ())]] if mp else [],
        "kb_plan": kp.name if kp else None,
        "kb_sub_query": kp.sub_query if kp else None,
        "kb_then": (getattr(kp, "then", None).sub_query if kp and getattr(kp, "then", None) else None),
        "route_override": ro["route"] if ro else None,
    }
json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(OUT, len(out))
