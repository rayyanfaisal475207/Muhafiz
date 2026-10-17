"""Inspect configured wrapper documentation without sending case data or secrets."""
import asyncio
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit,urlunsplit

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from dotenv import load_dotenv
import httpx


async def main():
    load_dotenv(ROOT/'.env')
    from src import config
    selected=urlsplit(config.LOCAL_LLM_URL)
    metadata=urlunsplit((selected.scheme,selected.netloc,'/openapi.json','',''))
    async with httpx.AsyncClient(timeout=15) as client:
        response=await client.get(metadata)
    if response.status_code!=200:
        print(json.dumps({'documentation_available':False,'http_status':response.status_code}));return
    body=response.json();schemas=body.get('components',{}).get('schemas',{})
    fields=[]
    for operation in body.get('paths',{}).get(selected.path,{}).values():
        if not isinstance(operation,dict):continue
        schema=operation.get('requestBody',{}).get('content',{}).get('application/json',{}).get('schema',{})
        if '$ref' in schema: schema=schemas.get(schema['$ref'].split('/')[-1],{})
        fields.extend(schema.get('properties',{}))
    output={'documentation_available':True,'request_fields':sorted(set(fields)),
            'thinking_control_documented':any(key in fields for key in ['think','enable_thinking','chat_template_kwargs']),
            'schema_control_documented':any(key in fields for key in ['json_schema','response_format','format']),
            'note':'Documentation is a capability hint; effective behavior still requires a synthetic probe.'}
    print(json.dumps(output))
    destination=ROOT/'data/qa/workflow_v2/model_capabilities.json'
    destination.write_text(json.dumps(output,indent=2),encoding='utf-8')


if __name__=='__main__':asyncio.run(main())
