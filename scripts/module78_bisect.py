import asyncio, json, sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.pipeline.query_rewriter import rewrite_query
from src.pipeline.router import route_query

QS = {
 "KB3": "Does the law expect the officer who first registers a case to be the same one who investigates it, or are those meant to be separate roles — and does that match what actually happens in our data?",
 "KB9": "Jab koi shakhs mashkook halaat mein foat ho jaye, to police ko maut ki wajah ki baaqaida tehqeeqaat karni hoti hai — kya hamara system yeh kahin darj karta hai, khaas tor par jab hamare itne cases mein maut shamil hai?",
 "P-KB3": "Under police law, is the person who records an FIR supposed to be a different officer from the one who investigates it — and what does our own data actually show about that?",
 "P-KB9": "When a death looks suspicious the police must formally investigate the cause of death — does our system record that anywhere?",
}

async def main():
    out = []
    for qid, q in QS.items():
        for run in range(3):
            rw = await rewrite_query(q, [])
            r_gold = await route_query(q)
            r_rw = await route_query(rw)
            rec = {"id": qid, "run": run+1, "rewritten": rw,
                   "route_on_gold": r_gold["route"], "conf_gold": r_gold.get("confidence"),
                   "route_on_rewritten": r_rw["route"], "conf_rw": r_rw.get("confidence"),
                   "reason_rw": r_rw.get("reason")}
            out.append(rec)
            print(json.dumps(rec, ensure_ascii=False), flush=True)
    json.dump(out, open("docs/gold-qa-wave2-results/module78_bisect.json","w",encoding="utf-8"), ensure_ascii=False, indent=1)

asyncio.run(main())
