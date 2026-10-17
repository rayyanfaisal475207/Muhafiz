"""Replay explicit conversation scenarios through an authenticated staging API.

Set EVIDENCE_REPLAY_TOKEN in the environment. Input JSON is a list of
{id, case_id?, project_id?, turns:[{message:...}, ...]}. Each scenario creates a
fresh session; preceding turns are executed, not injected as trusted state.
This writes chat sessions on the target server. Use a staging instance.
No authentication, data gateway or model dependencies are mocked.
"""
import argparse
import asyncio
from datetime import datetime,timezone
import json
import os
import time
from pathlib import Path
from uuid import uuid4

import httpx


async def wait_for_run(client,run_id,timeout=600,poll_interval=5):
    deadline=time.monotonic()+timeout
    while True:
        response=await client.get(f'/api/evidence/runs/{run_id}')
        response.raise_for_status()
        run=response.json()
        if run['status'] not in {'pending','running'} or time.monotonic()>=deadline: return run
        await asyncio.sleep(min(poll_interval,max(0,deadline-time.monotonic())))


async def replay(client,scenario,*,previous=None,pending_timeout=600):
    session=previous['session_id'] if previous else str(uuid4())
    records=list(previous['turns']) if previous else []
    if records and records[-1]['run']['status'] in {'pending','running'}:
        records[-1]['run']=await wait_for_run(client,records[-1]['run']['run_id'],pending_timeout)
        if records[-1]['run']['status'] in {'pending','running'}:
            return {'id':scenario['id'],'mode':'authenticated_api_replay','session_id':session,'turns':records,'waiting':True}
    if any(record['request']['message']!=turn['message'] for record,turn in zip(records,scenario['turns'])):
        raise ValueError('Resume manifest changed an already executed conversation turn')
    for turn in scenario["turns"][len(records):]:
        payload={"session_id":session,"message":turn["message"],
                 "case_id":scenario.get("case_id"),"project_id":scenario.get("project_id"),
                 "enable_web_search":False}
        events=[]
        async with client.stream("POST","/api/chat",json=payload) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))
        run_id=next((e["run_id"] for e in reversed(events) if e.get("run_id")),None)
        if not run_id:
            raise RuntimeError("Chat did not return an evidence run identifier")
        run=await wait_for_run(client,run_id,pending_timeout)
        records.append({"request":payload,"events":events,"run":run})
        if run["status"] in {"pending","running"}:
            # Persist the actual session/run; --resume polls it before sending
            # any remaining turn. A bounded timeout never means completion.
            break
    return {"id":scenario["id"],"mode":"authenticated_api_replay","session_id":session,"turns":records}


async def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest",type=Path)
    parser.add_argument("--base-url",required=True)
    parser.add_argument('--pending-timeout',type=float,default=600)
    parser.add_argument('--resume',type=Path,help='Previous results.json; continues the same sessions without reposting turns')
    args=parser.parse_args()
    if not 0 <= args.pending_timeout <= 3600: parser.error('pending-timeout must be between 0 and 3600 seconds')
    token=os.environ.get("EVIDENCE_REPLAY_TOKEN")
    if not token:
        parser.error("Set EVIDENCE_REPLAY_TOKEN; credentials are never taken as command-line arguments")
    output=Path("data/qa/replays")/(datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")+"_"+uuid4().hex[:8])
    output.mkdir(parents=True,exist_ok=False)
    records=[]
    previous={r['id']:r for r in json.loads(args.resume.read_text(encoding='utf-8'))} if args.resume else {}
    import secrets
    csrf=secrets.token_urlsafe(32)
    async with httpx.AsyncClient(base_url=args.base_url,
            headers={"Authorization":"Bearer "+token,'x-csrf-token':csrf},
            cookies={'csrf_token':csrf},timeout=110) as client:
        for scenario in json.loads(args.manifest.read_text(encoding="utf-8")):
            records.append(await replay(client,scenario,previous=previous.get(scenario['id']),pending_timeout=args.pending_timeout))
            (output/"results.json").write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding="utf-8")
    print(f"Replay saved to {output}; correctness remains subject to source review.")


if __name__=="__main__":
    asyncio.run(main())
