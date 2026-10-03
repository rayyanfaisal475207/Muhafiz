"""Versioned attachment ingestion and compatibility for older virtual spans."""
from __future__ import annotations

from .ingestion import stable_id
from .types import SourceSpan


async def sync_attachments(conn, scope, attachments):
    """Trusted ingestion; original revisions remain stored after removal."""
    from uuid import UUID
    from .ingestion import put_document
    accepted=[]
    for item in attachments:
        if (UUID(str(item['user_id'])) != scope.user_id
                or UUID(str(item['session_id'])) != scope.session_id):
            raise ValueError('Attachment ownership does not match the conversation')
        accepted.append(str(UUID(str(item['attachment_id']))))
    async with conn.transaction():
        await conn.execute('''UPDATE evidence.documents
            SET source_metadata=source_metadata || '{"attachment_revoked":true}'::jsonb
            WHERE owner_id=$1 AND session_id=$2
              AND source_metadata->>'endpoint'='session_attachments'
              AND NOT (source_metadata->>'attachment_id'=ANY($3::text[]))''',
            scope.user_id,scope.session_id,accepted)
        documents=[]
        for item,attachment_id in zip(attachments,accepted):
            text=item.get('extracted_text') or ''
            if not text:continue
            doc=await put_document(conn,source_key=f'attachment:{scope.session_id}:{attachment_id}',
                text=text,title=item.get('filename') or 'Attachment',owner_id=scope.user_id,
                session_id=scope.session_id,project_id=scope.project_id,
                metadata={'endpoint':'session_attachments','attachment_id':attachment_id,
                          'attachment_revoked':False})
            documents.append(doc)
            await conn.execute('''INSERT INTO evidence.ingestion_jobs(document_id)
                VALUES($1) ON CONFLICT(document_id) DO NOTHING''',doc)
        return documents


async def ingest_session_attachments(scope, attachments):
    from . import database
    pool=await database.pool('ingest')
    async with pool.acquire() as conn:
        return await sync_attachments(conn,scope,attachments)


async def revoke_attachment(attachment_id, user_id):
    from uuid import UUID
    from . import database
    pool=await database.pool('ingest')
    async with pool.acquire() as conn:
        await conn.execute('''UPDATE evidence.documents
            SET source_metadata=source_metadata || '{"attachment_revoked":true}'::jsonb
            WHERE owner_id=$1 AND source_metadata->>'endpoint'='session_attachments'
              AND source_metadata->>'attachment_id'=$2''',UUID(str(user_id)),str(UUID(str(attachment_id))))


def attachment_passages(attachments: list[dict], query: str, limit: int = 5) -> list[SourceSpan]:
    terms = [x.casefold() for x in query.split() if len(x)>1]
    candidates = []
    for attachment in attachments:
        text = attachment.get("extracted_text") or ""
        doc_id = stable_id("attachment",attachment["attachment_id"])
        for start in range(0,len(text),1600):
            excerpt = text[start:start+1600]
            score = sum(t in excerpt.casefold() for t in terms)
            candidates.append((score, SourceSpan(span_id=stable_id("attachment_span",doc_id,start),
                 document_id=doc_id,start=start,end=start+len(excerpt),text=excerpt,
                 title=attachment.get("filename") or "Attachment")))
    candidates.sort(key=lambda item: item[0],reverse=True)
    return [item[1] for item in candidates[:limit]]


class WithAttachments:
    def __init__(self, store, attachments):
        self.store, self.attachments = store, attachments

    # The runtime sets `.trace` on whatever store it was given. This wrapper
    # is that store, so the attribute has to reach the one underneath or the
    # SQL runs untraced.
    @property
    def trace(self):
        return getattr(self.store, "trace", None)

    @trace.setter
    def trace(self, value):
        self.store.trace = value

    async def catalog(self,*args,**kwargs):
        return await self.store.catalog(*args,**kwargs)

    async def snapshot(self,*args,**kwargs):
        return await self.store.snapshot(*args,**kwargs)

    async def field_meanings(self,*args,**kwargs):
        return await self.store.field_meanings(*args,**kwargs)

    async def resolve(self,*args,**kwargs):
        return await self.store.resolve(*args,**kwargs)

    async def resolve_mentions(self,*args,**kwargs):
        return await self.store.resolve_mentions(*args,**kwargs)

    async def ground_forms(self,*args,**kwargs):
        return await self.store.ground_forms(*args,**kwargs)

    async def execute(self, op, concepts):
        result = await self.store.execute(op,concepts)
        if (op.operation == "search" and op.source_scope == "all" and not op.source_collections
                and not (op.subject_type or op.entity_ids or op.filters or op.relationship_path)):
            spans = attachment_passages(self.attachments,op.text)
            result.citations.extend(spans)
            result.rows.extend({"span_id":str(s.span_id),"text":s.text,"title":s.title} for s in spans)
            if spans:
                result.status = "partial"
                result.coverage.notes.append("Attachment excerpts are limited to this conversation.")
        return result
