"""Select a verified gateway, preserving previous model configuration locally."""
import argparse
from datetime import datetime,timezone
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from dotenv import dotenv_values,set_key


def main():
    root=Path(__file__).resolve().parents[1]
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',required=True)
    args=parser.parse_args()
    base=args.url.rstrip('/')
    if urlsplit(base).scheme!='https' or not urlsplit(base).netloc:
        raise ValueError('Remote gateway must use HTTPS')
    key=os.environ['EVIDENCE_MODEL_API_KEY']
    updates={'LOCAL_LLM_URL':base+'/llm','LOCAL_GEN_LLM_URL':base+'/qalb',
        'LOCAL_LLM_MODEL':'qwen3:14b','LOCAL_GEN_LLM_MODEL':'mtaimoorhassan/qalb-llm-urdu-improved',
        'EVIDENCE_LOCAL_PROTOCOL':'custom','EVIDENCE_MODEL_WRAPPER_V2':'true',
        'EVIDENCE_MODEL_STRUCTURED_OUTPUT':'true','EVIDENCE_MODEL_THINK_CONTROL':'true',
        'EVIDENCE_MODEL_API_KEY':key}
    env=root/'.env'
    previous=dotenv_values(env)
    # Other model services may inherit this URL. Preserve their existing base.
    if not previous.get('MODEL_SERVER_BASE_URL') and previous.get('LOCAL_LLM_URL'):
        updates['MODEL_SERVER_BASE_URL']=previous['LOCAL_LLM_URL']
    destination=root/'data/qa/workflow_v2'/('gateway_config_'+datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')+'_'+uuid4().hex[:8]+'.json')
    destination.parent.mkdir(parents=True,exist_ok=True)
    with destination.open('x',encoding='utf-8') as stream:
        json.dump({'previous':{name:previous.get(name) for name in updates},
            'note':'Private rollback configuration; may contain a credential. Do not commit or share.'},stream,indent=2)
    for name,value in updates.items():set_key(str(env),name,value)
    print(json.dumps({'configured':True,'updated_fields':list(updates),'rollback_file':str(destination)}))


if __name__=='__main__':main()
