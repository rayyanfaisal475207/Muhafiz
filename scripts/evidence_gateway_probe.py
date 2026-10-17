"""Probe a deployed gateway using synthetic text only; never persist credentials."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import time
from uuid import uuid4

import httpx


async def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    key=os.environ['EVIDENCE_MODEL_API_KEY']
    base=args.url.rstrip('/')
    result={'synthetic_only':True,'checks':{},'native_gpu_release_verified':False}
    async with httpx.AsyncClient(timeout=45,headers={'Authorization':'Bearer '+key}) as client:
        async def post(body):
            started=time.monotonic()
            response=await client.post(base+'/generate',json=body)
            return response,round(time.monotonic()-started,3)
        response=await client.get(base+'/health')
        result['checks']['health']={'http_status':response.status_code,'body':response.json()}
        response=await client.get(base+'/health',headers={'Authorization':''})
        result['checks']['authentication']={'http_status':response.status_code,'passed':response.status_code==401}
        schema={'type':'object','properties':{'value':{'type':'integer','const':10}},'required':['value'],'additionalProperties':False}
        body={'model':'qwen3:14b','prompt':'Return the object with value 10.','json_schema':schema,'think':False,'timeout_seconds':35,'max_tokens':128}
        response,seconds=await post(body)
        data=response.json()
        structured=json.loads(data['response']) if response.status_code==200 else None
        result['checks']['structured']={'http_status':response.status_code,'seconds':seconds,
            'value':structured,'effective_controls':data.get('effective_controls'),
            'passed':response.status_code==200 and structured=={'value':10} and data.get('effective_controls')=={'think':False,'schema':True}}
        long_body={'model':'qwen3:14b','prompt':'Write a detailed ten thousand word essay on the integers and their mathematical properties.','think':False,'max_tokens':8192,'timeout_seconds':.1}
        response,seconds=await post(long_body)
        result['checks']['deadline']={'http_status':response.status_code,'seconds':seconds,'passed':response.status_code==504}
        request_id=str(uuid4())
        long_body.update(request_id=request_id,timeout_seconds=30)
        task=asyncio.create_task(post(long_body))
        cancellation=None
        try:
            for _ in range(15):
                await asyncio.sleep(.3)
                response=await client.post(base+'/requests/'+request_id+'/cancel')
                cancellation=response.json()
                if cancellation.get('cancel_requested') or task.done():break
            response,seconds=await task
            result['checks']['cancellation']={'http_status':response.status_code,'seconds':seconds,
                'cancel_requested':bool(cancellation and cancellation.get('cancel_requested')),
                'passed':response.status_code==499 and bool(cancellation and cancellation.get('cancel_requested'))}
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task,return_exceptions=True)
        response=await client.get(base+'/health')
        health=response.json()
        result['checks']['admission_recovery']={'http_status':response.status_code,'active':health.get('active'),'queued':health.get('queued'),
            'passed':response.status_code==200 and health.get('active')==0 and health.get('queued')==0}
        response,seconds=await post(body)
        result['checks']['post_cancel_generation']={'http_status':response.status_code,'seconds':seconds,'passed':response.status_code==200}
    result['passed']=all(check.get('passed',check.get('http_status')==200) for check in result['checks'].values())
    result['limitation']='HTTP cancellation and gateway admission recovery do not directly measure native GPU compute release.'
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result))
    return 0 if result['passed'] else 1


if __name__=='__main__':raise SystemExit(asyncio.run(main()))
