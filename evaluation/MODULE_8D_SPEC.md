# Module 8d — §154 body lost at full-document scale (KB extraction)

**Status:** ⬜ Not started — filed by Najiah, deferred out of Module 8c.
**Owner:** KB/ingestion pipeline owner (this is Module 8's chunking/Docling
territory, not 8c's retrieval-scoping charter).
**Blocks:** KB1 / KB3 fully passing (Contextual/KB legal questions). Does NOT
block Modules 14/15/16/9 — those touch `xagg.py`/`router.py`, a disjoint
subsystem. Only the final eval (Module 18) will show KB questions still weak
until 8d lands.

---

## The problem in one sentence

Even with Module 8c's batch size reduced to 32, the CrPC's **§154 body text**
("Every information relating to the commission of a cognizable offence … shall
be reduced to writing …") does **not** land in the stored KB chunks in a
**full-document** re-ingest — although the isolated Docling batch that
contains that page extracts the body correctly.

## Why this matters

KB1 asks which law governs FIR registration. The correct answer is CrPC §154.
Retrieval can only surface it if the §154 body is in a stored chunk. Today it
isn't — only the section *number* (as a heading/TOC reference) is stored, so
KB1 can't be answered from the corpus regardless of how well retrieval is
scoped (8c's scoping is correct and merged; this is the missing other half).

## Evidence gathered (all reproducible)

1. **The §154 body IS in the source PDF's text layer.** PyMuPDF reads it on
   page 77: *"Every information relating to the commission of a cognizable
   offence…"* — correct statutory text.

2. **Docling extracts the body correctly in isolation / small ranges.**
   `do_ocr=False` conversions of page ranges (76,79), (61,80), (41,80), and
   **(65,96)** — the exact batch-32 window page 77 falls into — all contain
   the §154 body. Verified directly against the converter.

3. **But the full 319-page re-ingest does NOT store it.** After a clean
   batch-32 re-ingest (2233 chunks):
   - `"reduced to writing"` appears in 4 chunks — but all are **§200**
     (examination of complainant, pages 101-103), NOT §154.
   - `"information relating to the commission of a cognizable"` → **0 chunks**.
   - `"if given orally to an officer in charge"` → **0 chunks**.
   - Page-77 stored chunks contain section *numbers* 149-154 (headings/TOC)
     but not the §154 body paragraph; page 78 jumps to 155-157.

4. **Chunking + normalization are NOT the culprit (ruled out).** A synthetic
   Document containing the §154 body, run through `normalize_whitespace` +
   `normalize_urdu` + `chunk_documents`, produces a chunk that DOES contain
   the body. So the transformation steps preserve it when the body is present
   in their input.

## The narrowed hypothesis (where to look next)

The body is lost **between `load_pdf`'s output and the stored chunks in the
full-document run specifically** — the one thing not yet isolated. Two
candidates, and the FIRST diagnostic below decides which:

- **(A) Docling-at-scale nondeterminism.** Converting the whole 319-page
  document (even in batches, within one process/converter) drops the body
  that an isolated batch keeps — i.e. `load_pdf`'s own page-77 Document is
  already missing the body in the full run. If so, the fix is in `load_pdf`
  (e.g. a fresh converter per batch, or smaller batches, or reconciling
  batch outputs against a text-layer check).

- **(B) `_group_pdf_pages()` concatenation drops it.** Module 8's page-
  concatenation (`chunker.py::_group_pdf_pages`) joins all pages before
  chunking; if the body is in `load_pdf`'s output but gone after grouping,
  the bug is there. (Note Module 8's own docstring says it FIXED the
  "heading in one chunk, body in another" split — but here the body is
  *absent*, not split, so this would be a different failure of the same
  function.)

## First diagnostic for whoever picks this up

Capture `load_pdf()`'s FULL-run output for the CrPC and check whether page
77's Document contains the §154 body **before** chunking:

```python
from src.ingestion.loaders.pdf_loader import load_pdf
docs = load_pdf(Path("Muhafiz_Knowledge_Base/1_1898_Code_of_Criminal_Procedure_(Pakistan).pdf"))
p77 = [d for d in docs if d.metadata.get("page") == 77]
print("§154 body in load_pdf p77:", any("every information relating to the commission" in d.text.lower() for d in p77))
```
- If **True** → the body survives extraction; the loss is in grouping/chunking
  → hypothesis (B), fix in `chunker.py`.
- If **False** → extraction itself drops it at full-document scale despite the
  isolated batch keeping it → hypothesis (A), fix in `load_pdf`.

(This run takes ~14 min on the current hardware — Docling layout on 319
pages. Run it in the background / with output to a file.)

## What Module 8c already shipped (so 8d composes cleanly)

- Retrieval **scoping** (legal Qs search the KB corpus first) — merged.
- **OCR-off** extraction for text-layer PDFs (correct + fast) — merged.
- **Batch size 32**, PyMuPDF **text-layer fallback**, **blank-page drop**, and
  **graceful vision-failure** so a large legal PDF ingests end-to-end without
  Gemini/quota fragility — merged.

When 8d makes the §154 body land in a chunk, KB1 works — 8c's scoping already
routes the legal question to that chunk.

## Environment notes

- Gemini free-tier daily quota (20 req/day) was exhausted during this work;
  8c's fixes mean the CrPC re-ingest no longer needs any vision call, so 8d
  diagnostics won't be quota-blocked.
- The CrPC is currently ingested (2233 chunks, is_global) via the batch-32
  path — index is healthy; 8d improves what's in those chunks, it doesn't
  need to restore anything.
- The other 6 KB PDFs were NOT re-ingested under 8c's new path yet (deferred);
  they still carry their older OCR-on chunks. Re-ingesting them is cheap
  follow-up work once 8d's approach is settled.
