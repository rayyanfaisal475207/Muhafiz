# Muhafiz — Design Rationale

A short account of *why* the interface looks and behaves the way it does, so you can
push back on the reasoning rather than just the pixels.

Nothing about the information architecture changed: sidebar → chat → pipeline trace,
same three columns, same order. Everything below is surface and behaviour.

---

## 1. Generation status: showing the work

**The problem.** The old UI showed a generic pulsing-dots placeholder for the entire
wait — which, on a RAG query with a retry loop, is 15–20 seconds of a spinner that
says nothing. The system was doing genuinely interesting work (rewriting the query,
routing, searching, *rejecting its own results and retrying*) and the user saw none
of it. Worse, that opacity is exactly where trust is won or lost for a product
citizens and officers rely on: "did it actually check the source, or is it improvising?"

**The approach.** The backend already streams every pipeline step over SSE. The UI now
translates engine vocabulary into human vocabulary and shows the current one:

| Engine step | What the user reads | Icon |
|---|---|---|
| `query_rewriter` | Understanding your question | spark |
| `router` | Choosing an approach | branching route |
| `retrieval` | Searching the knowledge base | magnifier w/ sweeping line |
| `web_search` | Searching the web | globe w/ drifting meridian |
| `reranker` | Ranking sources | sliders settling |
| `evaluator` | Reading results | open document, lines scanning |
| `response` | Writing response | pen drawing its own stroke |
| `file_generation` | Building your file | document assembling |
| `citation_validator` | Checking citations | shield + check |
| `memory` | Saving to this conversation | bookmark |

**Why these icons.** Each one animates *the part that carries the meaning* — the search
line sweeps, the globe's meridian drifts, the pen draws its stroke — rather than
spinning the whole glyph. A spinner communicates "wait"; these communicate "here is
what is happening". They're 1.5px line icons with no fills or gradients, so at 15px
they read as quiet UI furniture, not decoration. `title_generation` is deliberately
**not** in the table: it's housekeeping, not reasoning, and showing it would be noise.

**Why it collapses.** The status line lives *above* the answer while there is no answer.
The instant the first token arrives it collapses to one quiet line —
`Show reasoning · 7 steps · 5.3s` — because at that moment the user's attention should
be on the answer, not on the machinery. Expanding it reveals the full trail with
per-step durations and details (including retries, and *why* the evaluator rejected the
first attempt). The trail persists after the fact, so the reasoning is always available
and never in the way.

**Verified live:** phases cycled `Understanding your question → Choosing an approach →
Searching the knowledge base → Reading results`, then collapsed to
`Show reasoning · 7 steps · 5.3s` as the answer began streaming.

**Streaming.** Text was already token-by-token; the cursor is now a navy caret that
blinks on a 1.1s step, and each chunk fades in over 180ms so the text arrives softly
instead of snapping.

**A deliberate omission:** no progress bar and no percentage. We don't know how long a
retrieval will take, and a bar that lies is worse than no bar. The elapsed timer is
honest, and the small breadcrumb dots show how many phases are already done.

---

## 2. Theme: warm neutrals, one navy

**The palette is mostly not navy, and that's the point.** The discipline worth copying
from Claude isn't the specific hue — it's the *ratio*. Roughly 95% of the surface is
warm neutral; the accent appears only where it carries meaning. If navy were also the
sidebar, the headers, and the bubbles, then navy would mean nothing, and the "in
progress" indicator would have nothing to be louder than.

So the accent is reserved for exactly five things:
primary buttons · active nav/session/pipeline items · links · focus rings · in-progress indicators.

**The neutrals are warm** (`#F4F2ED` canvas, `#1C1B18` ink) rather than the blue-grays
the app used before. This is what makes the navy read as a deliberate accent rather than
"a slightly darker version of the background". A cool gray next to navy reads as one
muddy family; a warm gray next to navy reads as a decision.

**The navy is `#27477D` — "ink navy".** Considered and rejected:
- `#0F1F3D` (the old brand navy): near-black at small sizes. As an accent it reads as
  "bold text", not as a colour. It survives as the *dark* end of the ink scale.
- A brighter `#3B82F6`: reads as generic-SaaS-blue and, next to warm neutrals, looks
  like an unstyled link.
- `#27477D` sits at ~8:1 contrast on the off-white canvas, so it passes AA as body text,
  as a link, *and* as a button fill with white text — one token, every job, no exceptions
  table.

In dark mode the accent **lightens** to `#7FA3DC`. A deep navy on a dark ground is
invisible; the token flips so "active" still means "the blue one" in both themes. Dark
surfaces are warm charcoal (`#1A1917`), not blue-black, for the same reason as above.

**Shadows** are warm-tinted and soft (`0 4px 14px rgba(28,27,24,0.06)`) — the UI should
look like paper under diffuse light, not like cards floating in a black void. Radii are
generous and consistent (8/12/16px), issued from tokens so nothing drifts.

**Everything is a token.** No component carries a raw hex any more. A theme change is now
a change to `index.css`, not a hunt through 30 files. (The old code had `#0F1F3D` and
`#D4A017` hardcoded across the sidebar, and blue `rgba(59,130,246,…)` hardcoded in
bubbles — which is precisely why the app had light-on-light and invisible-text bugs.)

**Accessibility:** one focus style everywhere (a navy ring, never the browser default),
and all motion is disabled under `prefers-reduced-motion` — the state changes still
happen, just without the movement.

---

## 3. Logo: a four-point spark

**Direction.** Geometric, abstract, calm. Explicitly avoided: robots, speech bubbles,
brains, magnifying glasses, and the generic "AI sparkle" cluster — every AI product ships
those, and for a law-enforcement-adjacent product the connotation you want is *precision
and trust*, not *novelty*. This mark predates the rename from TaxIQ and was kept as-is —
it's abstract geometry with no wordplay tying it to either name, and it carries none of
the connotations (crest, badge, official seal) that would need client sign-off.

**The mark** is a single four-point spark with concave edges — a diamond that has been
pulled inward. It reads three ways, all of them on-message: a spark of insight, a
compass/precision mark, and (at 16px, where detail dies) a solid, calm diamond. One
closed path, no gradients, so it survives a favicon, a stamped PDF, and a monochrome fax.

**The tile** has three rounded corners and one square corner (top-left). That asymmetry is
the only "clever" move in the identity: it hints at a sheet of paper — a document, a
return, a filing — without drawing one. It also makes the icon recognisable in a row of
otherwise-identical rounded app squares.

**The lockup** is `[tile] Muhafiz`, wordmark in a single warm-ink colour. The old TaxIQ
lockup split the name two-tone (`Tax` in ink, `IQ` in navy) because the accent could land
on the half of the name that carried the product's promise — "Muhafiz" doesn't have an
equivalent internal split, so the rename dropped the two-tone treatment rather than force
an arbitrary break in the word. The mark itself (tile + spark) is unchanged.

**Variants shipped** (`frontend/public/brand/`):

| File | Use |
|---|---|
| `muhafiz-mark.svg` | app icon / favicon — navy tile, off-white spark |
| `muhafiz-mark-navy.svg` | glyph only, on light surfaces |
| `muhafiz-mark-light.svg` | glyph only, on dark surfaces |
| `muhafiz-lockup-light-bg.svg` | horizontal lockup, light backgrounds |
| `muhafiz-lockup-dark-bg.svg` | horizontal lockup, dark backgrounds (tile inverts, accent lifts) |

In-app, the mark is an inline React component (`components/brand/Logo.tsx`) so it inherits
theme tokens and never flashes in late. The files above are the hand-off assets.

**One caveat:** the wordmark in the SVG files uses live `<text>` with an Inter fallback so
it stays editable. Before sending these to a printer or an agency, outline the text —
otherwise a machine without Inter will silently substitute a different face.

---

## Open questions for you

1. **Sidebar.** It's now warm off-white like the rest of the app. The old navy slab was
   more distinctive but fought the calm; I'd keep it light, but if you want the navy rail
   back as a brand anchor, that's a one-token change.
2. **Phase labels.** "Reading results" is my translation of `evaluator`. If your users are
   officers who'd rather see the machinery ("Checking whether the retrieved sections
   actually answer this"), the label table is one object in `GenerationStatus.tsx`.
3. **Assistant bubble.** I removed the container entirely — the answer is now plain text on
   the surface (the way Claude presents responses), and only the *user's* turn gets a
   bubble. It makes long, cited answers far more readable, but it is a bigger departure
   from the old look than anything else here.
4. **The spark.** If it reads as too close to the common "AI sparkle", the alternative I'd
   pursue is a geometric monogram built from a squared-off "T" aperture — less warm, more
   institutional. Say the word and I'll mock it up.
