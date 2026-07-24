# Documents in this app: two systems, deliberately separate

There are two ways a document can enter the app. They look similar and are not.
Confusing them is the failure mode this design exists to prevent.

|                      | **Knowledge base** | **Chat attachment** |
|----------------------|--------------------|---------------------|
| Who adds it          | Admin, from the admin panel | Any user, from the chat composer |
| What happens to it   | Chunked → embedded → indexed | Text extracted once |
| Where it lives       | ChromaDB collection (+ Postgres `documents` for metadata) | Postgres `session_attachments` |
| Who can retrieve it  | **Everyone** — it answers all users' questions | Only that one conversation |
| Lifetime             | Permanent, shared | Dies with the conversation |
| API                  | `POST /api/admin/kb/upload` | `POST /api/attachments` |

## Why they can't leak into each other

The separation is **structural, not a filter**. A chat attachment is never
embedded and never written to the Chroma collection retrieval searches. It
cannot be returned to another user by a search, because it is not in the thing
being searched. There is no flag to forget to set.

Attachment text reaches the model by a different route: it is injected into the
prompt for its own conversation (`build_attachment_context` in
`src/api/attachments.py`), clearly labelled as user-supplied and *not* part of
the knowledge base, and capped so a large PDF cannot crowd out the documents
retrieved from the knowledge base.

`tests/test_attachments.py` pins this down. If someone later routes attachments
through the ingestion pipeline "to make them searchable", those tests fail.

## What this replaced

Before, the user-facing app had an "Ingest Files" page and a knowledge-base
manager. Two problems:

1. **It didn't work.** The drag-and-drop staged files in the browser and then
   called `POST /api/ingest`, which only re-scanned the *server's*
   `data/documents/` folder. Nothing was ever uploaded. Users believed they had
   added a document; they had not.
2. **It shouldn't have existed.** Anything ingested landed in the single shared
   corpus with no owner scoping, so one user's file would have been served to
   every other user as if it were verified reference material. That is not a
   feature you want on a compliance product.

Ingestion is now admin-only, and it really uploads.

## Adding to the knowledge base (admin)

Admin panel → **Knowledge Base** → drop a file. Supported: PDF, DOCX, XLSX, CSV,
HTML, Markdown, plain text, and images (read via vision OCR).

The file is written to `data/documents/`, then chunked and embedded in the
background. Progress is tracked per file in `ingestion_jobs` and shown on the
page as **processing → success / failed**, with the reason on failure. Chunks
land in the *existing* ChromaDB collection with `is_global = true` in their
metadata; there is no second store.

## Attaching a file to a conversation (user)

Chat composer → paperclip (or drag onto the composer). Limits: 10 MB, 5 files
per conversation, 12k characters of extracted text per file. A file that yields
no readable text is shown as a failed chip with the reason, rather than
disappearing.

## Migration

Attachments and ingestion status need tables from
`migrations/003_admin_dashboard_and_attachments.sql`. Until it is applied:

* attaching a file returns a clear "run migration 003" message (not a 500),
* the admin dashboard shows an *Instrumentation not applied* banner instead of
  empty charts that would read as a healthy, silent system,
* everything else — chat, retrieval, admin metrics — works as normal.

Apply it with
`python scripts/apply_migration.py migrations/003_admin_dashboard_and_attachments.sql`.

## A note on the existing corpus

A bug in the chunker meant every chunk was written under its own synthetic
`doc_id` (`unknown_<chunk_id>`), so the `documents` table holds one near-empty
row per chunk for everything ingested before this change. Retrieval was
unaffected (it reads the Chroma collection directly), but "chunks per document"
was meaningless.

The chunker now carries the parent `doc_id`, so new uploads produce one document
row with a real chunk count. The dashboard reports chunks per document by
grouping on `source_file`, which is correct for the old rows and the new ones
alike. If you want the legacy `documents` rows tidied up, re-ingesting those
files is the clean way to do it.
