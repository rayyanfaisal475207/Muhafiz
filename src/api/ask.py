"""Compatibility adapters. There is only one server-selected chat runtime."""
from __future__ import annotations

import json
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from src.auth.routes import get_current_user

router = APIRouter(tags=["ask"])


class AskRequest(BaseModel):
    question: str = Field(min_length=1,max_length=2000)
    session_id: str | None = None
    case_id: str | None = None
    project_id: str | None = None
    use_agent: bool = True  # Compatibility only; no unguarded fast path.


class StreamRequest(BaseModel):
    session_id: str | None = None
    message: str = Field(min_length=1,max_length=2000)
    case_id: str | None = None
    project_id: str | None = None


async def _delegate(request,payload,user):
    from src.main import ChatRequest,chat_endpoint
    data = payload.model_dump(exclude_unset=True,exclude={"question","use_agent"})
    data["session_id"] = payload.session_id or str(uuid4())
    data["message"] = payload.question if isinstance(payload,AskRequest) else payload.message
    return await chat_endpoint(request,ChatRequest(**data),user)


@router.post("/api/ask/stream")
async def ask_stream(request: Request,payload: StreamRequest,current_user=Depends(get_current_user)):
    from src import config
    if not config.EVIDENCE_RUNTIME_ENABLED:
        from . import ask_legacy
        return await ask_legacy.ask_stream(ask_legacy.StreamRequest(**payload.model_dump()),current_user)
    return await _delegate(request,payload,current_user)


@router.post("/api/ask")
async def ask(request: Request,payload: AskRequest,current_user=Depends(get_current_user)):
    from src import config
    if not config.EVIDENCE_RUNTIME_ENABLED:
        from . import ask_legacy
        return await ask_legacy.ask(ask_legacy.AskRequest(question=payload.question,
            case_id=payload.case_id,use_agent=payload.use_agent),current_user)
    response = await _delegate(request,payload,current_user)
    answer, citations, result = [], [], {}
    buffer = ""
    async for chunk in response.body_iterator:
        buffer += chunk.decode() if isinstance(chunk,bytes) else chunk
        while "\n\n" in buffer:
            block,buffer = buffer.split("\n\n",1)
            for line in block.splitlines():
                if not line.startswith("data: "):
                    continue
                event = json.loads(line[6:])
                if event.get("step") == "response" and event.get("status") == "streaming":
                    answer.append(event.get("detail") or "")
                citations.extend(event.get("sources") or [])
                for key in ("run_id","answer_status","coverage","clarification","artifact_url"):
                    if key in event:
                        result[key] = event[key]
    return {"question":payload.question,"answer":"".join(answer),"citations":citations,
            "route":"shared_chat",**result}


@router.get("/api/ask/health")
async def ask_health(current_user=Depends(get_current_user)):
    from src.api.evidence import health
    return await health(current_user)
