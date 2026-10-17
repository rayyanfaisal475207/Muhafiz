"""Bounded, local-only, read-only reproduction of the existing descriptive benchmark.

Captures stage timings and the actual descriptive model output before parsing.
Does not alter prompts, token budgets, retrieval, or the benchmark's scoring.
"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
load_dotenv('.env')
from scripts import descriptive_bench as bench
from src import config
from src.retrieval import engine,describe,agent
import asyncpg


async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--only',default='theft')
    parser.add_argument('--deadline',type=float,default=120)
    args=parser.parse_args()
    config.AIR_GAP_MODE=True  # No cloud fallback, even if .env configured one.
    directory=Path('data/descriptive_traces')/time.strftime('%Y%m%dT%H%M%S')
    directory.mkdir(parents=True,exist_ok=False)
    records=[];active=None
    def wrap(module,name):
        original=getattr(module,name)
        async def traced(*positional,**keyword):
            started=time.monotonic();event={'stage':name};active['events'].append(event)
            print(json.dumps({'case':active['case'],'started':name}),flush=True)
            try:
                result=await original(*positional,**keyword)
                if name=='call_llm':
                    event['max_tokens']=keyword.get('max_tokens')
                    event['response_chars']=len(result or '')
                    (directory/(active['case']+'_describe_raw.txt')).write_text(result or '',encoding='utf-8')
                if name=='hybrid_search':
                    event['span_ids']=[p.span_id for p in result.passages]
                if name=='describe':
                    event.update(findings=len(result.findings),dropped=result.dropped,refusal=result.refusal)
                return result
            except BaseException as exc:
                event['failure']=type(exc).__name__;raise
            finally:
                event['seconds']=round(time.monotonic()-started,3)
        setattr(module,name,traced)
    for name in ('enhance','build_plan','interpret','hybrid_search','decompose'):wrap(engine,name)
    wrap(describe,'describe');wrap(describe,'call_llm');wrap(agent,'run_agent')
    conn=await asyncpg.connect(engine.dsn())
    try:
        for case in bench.CASES:
            if args.only not in case.name:continue
            active={'case':case.name,'question':case.question,'events':[],
                'describe_sha256':hashlib.sha256(Path(describe.__file__).read_bytes()).hexdigest()}
            records.append(active)
            try:
                async with conn.transaction(readonly=True):
                    await conn.execute('SET LOCAL statement_timeout=15000')
                    async with asyncio.timeout(args.deadline):
                        result=await bench.check(conn,case)
                        active.update(checks=result.checks,findings=result.findings,seconds=result.seconds,detail=result.detail)
            except TimeoutError:
                active['failure']='foreground_deadline'
            finally:
                (directory/'trace.json').write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding='utf-8')
                print(json.dumps({k:v for k,v in active.items() if k not in {'question','events'}},ensure_ascii=True),flush=True)
    finally:await conn.close()
    print('Trace saved to '+str(directory),flush=True)


if __name__=='__main__':asyncio.run(main())
