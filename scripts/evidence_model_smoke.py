"""Verify both configured model slots through Muhafiz using synthetic JSON."""
import asyncio
import json
from pathlib import Path
import sys
import time
from typing import Literal

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT/'.env',override=True)
from pydantic import BaseModel
from src.evidence.model import LocalModel


class SyntheticValue(BaseModel):
    value: Literal[10]


async def main():
    output=[]
    for slot,generation in [('reasoning',False),('generation',True)]:
        started=time.monotonic()
        model=LocalModel()
        value=await model.structured(SyntheticValue,'Return the requested synthetic object.',
            {'instruction':'Return value 10.'},generation=generation,deadline=time.monotonic()+40)
        output.append({'slot':slot,'value':value.model_dump(),'seconds':round(time.monotonic()-started,3)})
    destination=ROOT/'data/qa/workflow_v2/model_adapter_live_20260927.json'
    destination.write_text(json.dumps(output,indent=2),encoding='utf-8')
    print(json.dumps(output))


if __name__=='__main__':asyncio.run(main())
