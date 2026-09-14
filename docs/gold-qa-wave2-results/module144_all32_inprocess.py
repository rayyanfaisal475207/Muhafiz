"""All-32 in-process aggregate regression: run `run_aggregate()` for every
gold question against the LIVE graph + gateway and write the rendered text.
Run once with PYTHONPATH pointing at the pre-module tree and once at this
tree; diff the two files. Output: {id: {kind, text, filters}}.
"""
import asyncio, json, os, sys
sys.path.insert(0, os.getcwd())
from src.pipeline import xagg
from src.pipeline.harness.tools.xagg import _render_aggregate_text
from src.data_gateway.selector import get_gateway

OUT = sys.argv[1]
GOLD = "evaluation/Gold_QA_Dataset_Final32_With_Answers.json"


async def main():
    gw = await get_gateway()
    items = json.load(open(GOLD, encoding="utf-8"))
    extra = json.load(open(os.environ.get("M144_PROBES", "scratchpad/probes_after.json"), encoding="utf-8"))
    for pid, spec in extra.items():
        items.append({"id": pid, "question": spec["question"] if isinstance(spec, dict) else spec})
    out = {}
    for it in items:
        q = it["question"]
        kind = xagg.resolve_aggregate_kind(q)
        try:
            r = await xagg.run_aggregate(q, None, gw, user_role="platform-admin")
            text = _render_aggregate_text(r)
            out[it["id"]] = {"dispatch": kind, "kind": r.get("kind"), "filters": r.get("filters_applied"), "text": text}
        except Exception as e:  # noqa
            out[it["id"]] = {"dispatch": kind, "error": f"{type(e).__name__}: {e}"}
        print(it["id"], kind, (out[it["id"]].get("text") or out[it["id"]].get("error", ""))[:90].replace("\n", " | "))
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

asyncio.run(main())
