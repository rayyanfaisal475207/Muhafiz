"""Private diagnostic capture of rejected plans for an approved history question."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT/'.env')
from src.evidence.model import LocalModel,ModelOutputError
from src.evidence.runtime import Runtime
from src.evidence.store import ScopedStore
from src.evidence.types import Scope
from scripts.evidence_history_check import QUESTIONS,all_case_ids


async def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--question-id',type=int,required=True)
    parser.add_argument('--output-dir',type=Path,required=True)
    args=parser.parse_args()
    args.output_dir.mkdir(parents=True,exist_ok=False)
    rejected=[]
    class CapturingModel(LocalModel):
        async def structured(self,contract,*a,**kw):
            try: return await super().structured(contract,*a,**kw)
            except ModelOutputError as exc:
                if contract.__name__=='QuestionPlan' and exc.raw:
                    rejected.append({'error':str(exc),'raw':exc.raw})
                    (args.output_dir/'rejected_plans.json').write_text(json.dumps(rejected,ensure_ascii=False,indent=2),encoding='utf-8')
                raise
    question=json.loads(QUESTIONS.read_text(encoding='utf-8'))[args.question_id-1]['question']
    scope=Scope(user_id=uuid4(),session_id=uuid4(),case_ids=await all_case_ids())
    answer=await Runtime(ScopedStore(scope),CapturingModel()).run(uuid4(),question,scope,deadline=time.monotonic()+90)
    (args.output_dir/'answer.json').write_text(answer.model_dump_json(indent=2),encoding='utf-8')
    print('Status:',answer.status,'rejected plans:',len(rejected))


if __name__=='__main__': asyncio.run(main())
