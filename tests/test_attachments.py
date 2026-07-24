"""
Chat attachments vs the knowledge base.

The whole point of the attachment design is that the two systems cannot leak
into each other. These tests pin that guarantee down:

  * an attachment is never chunked, embedded, or written to the ChromaDB collection
  * an attachment's text reaches only its own conversation's prompt
  * a knowledge-base upload IS chunked into the same table retrieval reads
  * one user's attachment is invisible to another user

If someone later "helpfully" routes attachments through the ingestion pipeline,
these fail.
"""
import uuid
from pathlib import Path

import pytest

import src.pipeline.orchestrator as orch
from src.api.attachments import build_attachment_context


@pytest.fixture
def session_with_attachment(gateway, user_id, session_id):
    gateway.sessions[session_id] = {
        "session_id": session_id, "user_id": user_id, "project_id": None, "title": "T",
    }
    gateway.attachments = [{
        "attachment_id": str(uuid.uuid4()),
        "session_id": session_id,
        "user_id": user_id,
        "filename": "fir-copy.pdf",
        "file_type": "pdf",
        "status": "ready",
        "extracted_text": "FIR No. 412/2026. Complainant reports mobile phone theft on 12 May 2026.",
        "char_count": 52,
    }]
    return gateway


# ── The separation guarantee ─────────────────────────────────────────────────

async def test_attachment_text_never_enters_the_vector_store(patched_gateway, session_with_attachment, session_id):
    """
    An attachment is prompt context, not a corpus entry. Nothing about
    attaching a file may add a chunk to the table retrieval reads.
    """
    chunks_before = list(patched_gateway.chunks)

    context = await build_attachment_context(session_id)

    assert "FIR No. 412/2026" in context            # it did reach the prompt
    assert patched_gateway.chunks == chunks_before  # and nowhere near retrieval


async def test_attachment_context_is_empty_for_a_session_with_no_files(patched_gateway, session_id):
    assert await build_attachment_context(session_id) == ""


async def test_attachment_context_labels_the_file_and_disclaims_the_knowledge_base(
    patched_gateway, session_with_attachment, session_id,
):
    """The model must not cite an attachment as if it were the knowledge base."""
    context = await build_attachment_context(session_id)

    assert "fir-copy.pdf" in context
    assert "not part of the knowledge base" in context


async def test_a_failed_attachment_contributes_nothing(patched_gateway, session_id):
    patched_gateway.attachments = [{
        "attachment_id": str(uuid.uuid4()), "session_id": session_id,
        "filename": "scan.png", "status": "failed",
        "extracted_text": None, "error_message": "No readable text",
    }]

    assert await build_attachment_context(session_id) == ""


async def test_attachment_context_is_capped(patched_gateway, session_id):
    """A 300-page PDF must not evict the retrieved documents from the prompt."""
    patched_gateway.attachments = [{
        "attachment_id": str(uuid.uuid4()), "session_id": session_id,
        "filename": "huge.pdf", "status": "ready",
        "extracted_text": "x" * 100_000,
    }]

    context = await build_attachment_context(session_id, max_chars=1000)

    assert len(context) < 2000


async def test_a_missing_attachments_table_does_not_break_chat(patched_gateway, session_id, monkeypatch):
    """Before migration 003 runs, chat must still work — just without attachments."""
    async def _boom(*args, **kwargs):
        raise RuntimeError('relation "session_attachments" does not exist')

    monkeypatch.setattr(patched_gateway, "get_attachments_for_session", _boom)

    assert await build_attachment_context(session_id) == ""


# ── The attachment reaches the answer ────────────────────────────────────────

async def test_the_model_sees_the_attached_file(monkeypatch, patched_gateway, session_with_attachment, session_id, user_id):
    """End to end: attach a file, ask a question, the text is in the system prompt."""
    captured = {}

    async def fake_call_llm(system_prompt, user_message, **kwargs):
        sp = (system_prompt or "").lower()
        if "routing engine" in sp:
            return '{"route": "DIRECT", "output_format": "chat"}'
        if "rewrit" in sp or "search-query" in sp:
            return user_message
        return "ok"

    async def fake_stream_llm(system_prompt, user_message, **kwargs):
        captured["system"] = system_prompt
        yield "The FIR number is 412/2026."

    monkeypatch.setattr(orch, "call_llm", fake_call_llm)
    monkeypatch.setattr(orch, "stream_llm", fake_stream_llm)
    monkeypatch.setattr("src.pipeline.router.call_llm", fake_call_llm)
    monkeypatch.setattr("src.pipeline.query_rewriter.call_llm", fake_call_llm)
    monkeypatch.setattr("src.pipeline.title_generator.call_llm", fake_call_llm)
    for fn in ("upsert_session", "create_query", "log_step", "log_llm_call",
               "log_retrieved_docs", "update_retrieved_docs_relevance", "update_query"):
        monkeypatch.setattr(orch.pipeline_logger, fn, lambda *a, **k: 1, raising=False)

    events = []
    async for event in orch.process_query(
        session_id, "What is the FIR number on my attached file?", user_id=user_id,
    ):
        events.append(event)

    assert "FIR No. 412/2026" in captured["system"], "the attached file never reached the model"
    assert any(e["step"] == "attachments" for e in events), "the UI was never told a file was read"


# ── The knowledge base still behaves like a knowledge base ───────────────────

async def test_kb_ingestion_writes_chunks_to_the_retrieval_table(monkeypatch, tmp_path):
    """
    The other half of the contract: an ADMIN upload *is* chunked into the same
    table retrieval reads (is_global), so every user can be answered from it.
    """
    from src.ingestion import service

    doc = tmp_path / "offense_sections.txt"
    doc.write_text("Section 379 PPC. Theft of movable property is punishable by imprisonment." * 20, encoding="utf-8")

    written = {}

    async def fake_embed_texts(texts, task_type=None):
        return [[0.1] * 8 for _ in texts]

    async def fake_upsert(ids, texts, embeddings, metadatas):
        written["ids"] = ids
        written["metadatas"] = metadatas

    monkeypatch.setattr(service, "embed_texts", fake_embed_texts)
    monkeypatch.setattr(service, "upsert_documents", fake_upsert)

    class _Gateway:
        async def log_document(self, **kwargs):
            written["document"] = kwargs

    async def fake_get_gateway():
        return _Gateway()

    monkeypatch.setattr("src.data_gateway.get_gateway", fake_get_gateway)

    stats = await service.ingest_file(doc, is_global=True)

    assert stats["chunks_added"] > 0
    assert written["ids"], "no chunks were written to the vector store"
    assert all(m["is_global"] for m in written["metadatas"]), "KB documents must be global"
    assert written["document"]["chunk_count"] == stats["chunks_added"]


async def test_ingestion_reports_a_readable_failure(monkeypatch, tmp_path):
    """
    Regression: a NameError in vector_store meant every ingestion reported
    "0 chunks added" while silently succeeding. Failures must be explicit.
    """
    from src.ingestion import service

    empty = tmp_path / "empty.txt"
    empty.write_text("", encoding="utf-8")

    stats = await service.ingest_file(empty, is_global=True)

    assert stats["chunks_added"] == 0
    assert stats.get("error"), "a failed ingestion must say why"
