"""Probe configured GPU contracts using synthetic evidence only.

No original case text, database credentials or endpoint URLs are printed.
Raw model responses stay under ignored data/evidence_probe for diagnosis.
"""
import asyncio
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
import httpx
from src.evidence.ingestion import DISCOVER,REVIEW,Extraction,ExtractionReview,discover
from src.evidence.model import LocalModel


class DiagnosticTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.transport=httpx.AsyncHTTPTransport()
        self.calls=[]

    async def handle_async_request(self,request):
        response=await self.transport.handle_async_request(request)
        await response.aread()
        data=response.json()
        raw=data.get("response","")
        self.calls.append({"http_status":response.status_code,"response":raw,
            "answer_chars":len(raw or ""),"thinking_chars":len(data.get("thinking") or ""),
            "eval_count":data.get("eval_count"),"finish_reason":data.get("finish_reason")})
        return response

    async def aclose(self):
        await self.transport.aclose()


async def main():
    load_dotenv(Path(__file__).resolve().parents[1]/".env")
    payload={"document":"Synthetic test record: FIR 12/2026 was registered at Alpha station. "
             "Police recovered 1 pistol of 30 bore and 6 cartridges. Alpha station is in Test City.",
             "catalog":[],"requested_concepts":[]}
    reports=[]
    for stage in ("discovery","review"):
        transport=DiagnosticTransport()
        try:
            model=LocalModel(transport=transport)
            if stage=="discovery":
                extraction=await discover(model,payload,deadline=time.monotonic()+90)
                payload["proposed"]=extraction.model_dump(mode="json")
                result={"concepts":len(extraction.concepts),"entities":len(extraction.entities),"facts":len(extraction.facts)}
            elif "proposed" in payload:
                review=await model.structured(ExtractionReview,REVIEW,payload,
                    generation=os.getenv("EVIDENCE_REVIEW_MODEL","reasoning")=="generation",deadline=time.monotonic()+60)
                result={"reviewed_facts":len(review.facts),"entire_document_checked":review.entire_document_checked}
            else:
                break
            reports.append({"stage":stage,"passed":True,**result})
        except Exception as exc:
            reports.append({"stage":stage,"passed":False,"error":str(exc)[:600]})
        directory=Path("data/evidence_probe");directory.mkdir(parents=True,exist_ok=True)
        (directory/(stage+".json")).write_text(json.dumps(transport.calls,ensure_ascii=False,indent=2),encoding="utf-8")
        reports[-1]["calls"]=[{k:v for k,v in call.items() if k!="response"} for call in transport.calls]
        print(json.dumps(reports[-1]),flush=True)
    Path("data/evidence_probe/report.json").write_text(json.dumps(reports,indent=2),encoding="utf-8")


if __name__=="__main__":asyncio.run(main())
