"""Artifacts are built only from the already-rendered, checked answer."""
from __future__ import annotations

import asyncio
from uuid import uuid4

from .database import connection


async def create_report(answer, scope):
    if not answer.plan or answer.plan.output_format == "chat" or answer.status not in {"answered", "partial"}:
        return answer
    sections = [{"type":"paragraph","content":answer.text}]
    citations = {s.span_id:s for r in answer.results for s in r.citations}
    for span in citations.values():
        sections.append({"type":"paragraph","content":f"[{span.span_id}] {span.title}: {span.text}"})
    sections.append({"type":"table","headers":["Result", "Source", "Evidence"],
                     "rows":[["Checked answer", "", answer.text]] +
                     [[r.label,f"{s.title} [{s.span_id}]",s.text] for r in answer.results for s in r.citations]})
    payload = {"title":"Muhafiz", "description":answer.text,"sections":sections}
    if answer.plan.output_format == "pdf":
        from src.generation.pdf_builder import build_pdf as builder
    elif answer.plan.output_format == "docx":
        from src.generation.docx_builder import build_docx as builder
    else:
        from src.generation.xlsx_builder import build_xlsx as builder
    path,_ = await asyncio.to_thread(builder,payload)
    artifact_id = uuid4()
    async with connection(scope,write=True) as conn:
        await conn.execute("""INSERT INTO evidence.artifacts(artifact_id,run_id,user_id,path,format)
                            VALUES($1,$2,$3,$4,$5)""",artifact_id,answer.run_id,scope.user_id,path,answer.plan.output_format)
    answer.artifact_url = f"/api/evidence/artifacts/{artifact_id}"
    return answer
