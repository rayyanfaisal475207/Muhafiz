# Documents in this app: two systems, deliberately separate

There are two ways a *file* can enter the app. They look similar and are not.
Confusing them is the failure mode this design exists to prevent. A third,
non-file route — the Muhafiz Data API — is documented separately below; it
shares the knowledge base's storage and retrieval, but has no file at all.

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

## Ingesting from the Muhafiz Data API (no file involved)

See `docs/decisions/0001-muhafiz-api-migration.md` for the full background —
this section only documents the mechanics.

`src/ingestion/muhafiz_records.py` (record → `Document`) and
`src/ingestion/service.py`'s `ingest_documents()` (M2's file-agnostic entry
point) together let a REST record from the live Muhafiz Data API
(`API_CONSUMER_GUIDE.md`) reach the exact same knowledge base as an uploaded
file — same Chroma collection, same chunk/embed pipeline, same graph
extraction step — without ever touching `data/documents/` or
`route_and_load()`.

Only genuine free text is chunked and embedded (FIR narrative, zimni entries,
CMS complaint summaries, PKM loss/incident descriptions, roznamcha entries) —
never the structured fields (accused, sections, weapon rows, timestamps).
Those go straight to the graph as ground truth instead
(`src/graph/structured_projection.py`), which is the entire point of this
source: real identifiers don't need an LLM to re-guess them from prose.

Each rendered `Document`'s `source` metadata is a stable string —
`psrms/fir/{fir_id}#narrative`, `cms/complaint/{complaint_id}#summary`, etc.
— built only from fields the API guarantees are stable identifiers, never
from content. Re-fetching and re-rendering the same record must always
produce the same `source` (and therefore the same chunk `doc_id`s), or a
later sync run silently orphans the previous run's graph edges instead of
updating them in place.

Chunk metadata carries three extra fields no file-sourced chunk has:
`record_type`, `source_system` (`"muhafiz_api"`), `external_id`, plus
`station_code`/`district` where known and `content_provenance` (the
record's own `source: "synthetic"`/`"real"` tag from the API — named
differently from the existing `source` metadata key to avoid colliding with
"which record this chunk came from").

Bypassing `route_and_load()` also bypasses every check in
`src/ingestion/validation.py` — this route has no file-size/magic-byte
guard because those don't apply to a REST record; a record-count guard is
the caller's own responsibility (see `ingest_documents()`'s docstring).

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
