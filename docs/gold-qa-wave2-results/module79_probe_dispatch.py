import json, os, sys
sys.path.insert(0, os.getcwd())
from src.pipeline.xagg import resolve_aggregate_kind, resolves_to_specific_aggregate
from src.pipeline.harness.agents.meta_analysis import _match_decomposition_plan
from src.pipeline.harness.supervisor import _xagg_answers_in_one_call, classify_to_subagent
from src.pipeline import router
probes = json.load(open(sys.argv[1], encoding="utf-8"))
for pid, q in probes.items():
    ro = router._deterministic_route_override(q, case_id=None)
    plan = _match_decomposition_plan(q)
    sub = classify_to_subagent({"route": "XAGG", "case_scope": "cross_case"}, q)
    print(pid, "| override:", ro["route"] if ro else None, "| kind:", resolve_aggregate_kind(q),
          "| plan:", plan.name if plan else None, "| one_call:", _xagg_answers_in_one_call(q), "| sub-agent on XAGG:", sub)
