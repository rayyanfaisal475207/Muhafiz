# Module 8d — §154 body lost in chunking: ROOT-CAUSED & FIXED (code); live re-ingest deferred

**Status:** ✅ **Root cause found and fixed in code** (`21c53ca`, pushed).
⏳ **Live KB re-ingest + retrieval verification deferred** — blocked by this
machine's RAM, not by the fix.
**Owner:** Najiah (fix). Live re-ingest: whoever has a machine with more RAM.

---

## What the bug actually was (not what earlier modules thought)

Modules 8/8b/8c chased "§154's body missing from the KB" as an extraction or
chunk-boundary problem. It was neither. It was a **general chunker bug: the
text splitter silently DROPPED ~448-character spans of dense text** — never
emitting them in any chunk.

**Mechanism.** In `_split_text_into_chunks_with_offsets`
(`src/ingestion/chunker.py`), when a chunk ended early at a sentence boundary
(so `end - start` was well under `chunk_size`), the advance fallback did
`advance = min(chunk_size, text_len - start)` — striding a full `chunk_size`
forward from `start`, **leaping over the un-emitted tail** between `end` and
the new `start`. That text reached no chunk at all.

**Proof (deterministic, reproducible).** Tracing §154's body through the real
pipeline:
- present in `load_pdf` output (page 77) ✓
- present after `_group_pdf_pages` ✓
- present after normalization ✓ (at char 5406 of the 10184-char grouped text)
- **absent from all resulting chunks** ✗ — with a logged
  `GAP 5526..5974 (448 chars) skipped` covering the §154 body exactly.

So §154 was **deleted during chunking**, which is why no amount of extraction
(8b) / OCR-off (8c) / scoping (8c) work ever made KB1 pass — the content
wasn't in the store to retrieve.

## The fix (`21c53ca`)

Cap the fallback advance at `end - start` (advance TO the chunk's end, losing
only the overlap) so `start` never moves past the chunk just emitted. Every
character is now covered by some chunk regardless of where the sentence-
boundary snap lands.

```python
# before:
if advance < min_advance:
    advance = min(chunk_size, text_len - start)   # leaps over un-chunked tail
# after:
if advance < min_advance:
    advance = min(min_advance, end - start)       # never past the chunk end
```

**This is a general correctness fix, not legal-PDF-specific** — the same
silent drop affected any dense document across the whole corpus, so fixing it
likely improves retrieval quality broadly.

**Verified at the code level:**
- Deterministic diagnostic: §154's full statement now lands complete in ONE
  chunk; the ~448-char gaps are gone (chunk count rises as dropped content
  returns).
- Tests: 11 pass in `tests/test_ingestion_chunker.py` — 9 existing + 2 new
  no-gap coverage guarantees asserting every non-whitespace char appears in
  some chunk.

## What's left (deferred, environment-blocked — NOT a code issue)

To make KB1/KB3 actually retrieve §154 live, the KB PDFs must be **re-ingested**
so the fixed chunker's output reaches Chroma. On this machine that is blocked
by two environment problems, both unrelated to the fix:

1. **RAM.** The 319-page CrPC `load_pdf` (Docling layout on every page)
   repeatedly OOMs / dies mid-run on this ~4 GB-free machine — the same
   `std::bad_alloc`-class failure `scripts/_reextract_graph.py`'s own comment
   documents. A fresh-process-per-page-range approach helps but embedding 300+
   chunks per range via the model server is also slow.
2. **Chroma left in a bad state.** Processes killed mid-write (during the
   RAM-death attempts) left the `data/chroma_db` store with ~9 orphaned
   collection-segment directories; `collection.count()` / queries now **hang
   indefinitely**. This needs a clean Chroma rebuild before any re-ingest.

### To finish 8d on a stronger machine
1. **Repair/rebuild Chroma**: on a machine with adequate RAM, drop and recreate
   the `muhafiz_kb` collection (`scripts/reingest_kb.py` does a full wipe +
   re-ingest of everything in `config.DOCUMENTS_DIR`), OR clear the orphaned
   segment dirs and re-ingest per source.
2. **Re-ingest the 7 KB PDFs** with the fixed chunker active (the code is on
   `main`/the branch already). No Gemini vision calls are needed — 8c's
   text-layer fallback + blank-page drop handle the CrPC's cover/back-matter.
3. **Confirm** §154's body is in Chroma:
   `col.get(where={"source": "1_1898_Code_of_Criminal_Procedure_(Pakistan).pdf"})`
   should contain a chunk with "every information relating to the commission
   of a cognizable offence".
4. **Verify KB1 live** via `/api/chat` (admin@example.com / MuhafizAdmin2026!)
   — it should now cite CrPC §154/§155 for FIR-registration questions.
5. **Module 18**: re-run the KB questions (or full 32) — the KB bucket, which
   scored **0.00** in the Module 9 rerun, should lift now that the statutory
   text is retrievable.

## Bottom line

The hard part — finding and fixing the actual root cause — is **done, tested,
and shipped**. What remains is an operational re-ingest that this specific
machine can't run, not a code gap. On adequate hardware, steps 1–5 above are
mechanical and should move the KB bucket off zero.
