// TaxIQ product deck — generated with the product's own design tokens.
const path = "C:/Users/HP/AppData/Roaming/npm/node_modules/";
const PptxGenJS = require(path + "pptxgenjs");
const fs = require("fs");

const pres = new PptxGenJS();
pres.layout = "LAYOUT_WIDE";           // 13.33 x 7.5in
pres.author = "TaxIQ";
pres.title = "TaxIQ — Product Overview";

// ── Design tokens (identical to frontend/src/index.css) ──────────────────────
const NAVY   = "27477D";   // accent
const NAVY_D = "16294A";
const NAVY_L = "6B8FBF";
const INK    = "1C1B18";
const BODY   = "4B4842";
const MUTED  = "86827A";
const FAINT  = "A8A499";
const CANVAS = "F4F2ED";   // warm off-white
const CARD   = "FFFFFF";
const CARD2  = "FAF9F5";
const WELL   = "EFECE4";
const BORDER = "E5E1D8";
const GREEN  = "3F7D58";
const AMBER  = "B0762A";
const RED    = "B4443C";

const FONT = "Calibri";
const LOGO = "logo.png";

const W = 13.33, H = 7.5;
const M = 0.62;                         // page margin

const shadow = () => ({ type: "outer", color: "1C1B18", blur: 10, offset: 2, angle: 90, opacity: 0.06 });

let n = 0;

// ── Chrome: canvas, logo mark, slide number ─────────────────────────────────
function frame(slide, { dark = false } = {}) {
  n += 1;
  slide.background = { color: dark ? NAVY_D : CANVAS };
  slide.addImage({ path: LOGO, x: M, y: H - 0.62, w: 0.24, h: 0.24, transparency: dark ? 0 : 20 });
  slide.addText("TaxIQ", {
    x: M + 0.3, y: H - 0.63, w: 1.0, h: 0.26, margin: 0,
    fontFace: FONT, fontSize: 10, bold: true, color: dark ? "FFFFFF" : FAINT,
    valign: "middle",
  });
  slide.addText(String(n), {
    x: W - M - 0.6, y: H - 0.63, w: 0.6, h: 0.26, margin: 0,
    fontFace: FONT, fontSize: 10, color: dark ? NAVY_L : FAINT, align: "right", valign: "middle",
  });
  return slide;
}

function title(slide, text, sub) {
  slide.addText(text, {
    x: M, y: 0.5, w: W - 2 * M, h: 0.55, margin: 0,
    fontFace: FONT, fontSize: 32, bold: true, color: INK, valign: "middle",
  });
  if (sub) {
    slide.addText(sub, {
      x: M, y: 1.06, w: W - 2 * M, h: 0.3, margin: 0,
      fontFace: FONT, fontSize: 13, color: MUTED, valign: "middle",
    });
  }
}

// A card: subtle tint + soft shadow. No edge stripes.
function card(slide, x, y, w, h, fill = CARD) {
  slide.addShape(pres.ShapeType.roundRect, {
    x, y, w, h, rectRadius: 0.08,
    fill: { color: fill }, line: { color: BORDER, width: 1 }, shadow: shadow(),
  });
}

// The recurring motif: an icon glyph inside a soft navy circle.
function badge(slide, x, y, glyph, d = 0.36) {
  slide.addShape(pres.ShapeType.ellipse, {
    x, y, w: d, h: d, fill: { color: "FFFFFF" }, line: { color: NAVY, width: 1 },
  });
  slide.addText(glyph, {
    x, y, w: d, h: d, margin: 0,
    fontFace: FONT, fontSize: 12, bold: true, color: NAVY, align: "center", valign: "middle",
  });
}

// Pipeline / flow node
function node(slide, x, y, w, h, label, detail, opts = {}) {
  const fill = opts.accent ? NAVY : CARD;
  const fg = opts.accent ? "FFFFFF" : INK;
  slide.addShape(pres.ShapeType.roundRect, {
    x, y, w, h, rectRadius: 0.06,
    fill: { color: fill }, line: { color: opts.accent ? NAVY : BORDER, width: 1 }, shadow: shadow(),
  });
  slide.addText(label, {
    x, y: y + (detail ? 0.06 : 0), w, h: detail ? h * 0.55 : h, margin: 0,
    fontFace: FONT, fontSize: opts.small ? 10 : 11.5, bold: true, color: fg,
    align: "center", valign: "middle",
  });
  if (detail) {
    slide.addText(detail, {
      x, y: y + h * 0.5, w, h: h * 0.45, margin: 0,
      fontFace: FONT, fontSize: 9, color: opts.accent ? "CADCFC" : MUTED,
      align: "center", valign: "middle",
    });
  }
}

function arrow(slide, x, y, w, h, opts = {}) {
  slide.addShape(pres.ShapeType.line, {
    x, y, w, h,
    line: { color: opts.color || NAVY_L, width: 1.5, endArrowType: "triangle",
            dashType: opts.dash ? "dash" : "solid" },
  });
}

function stat(slide, x, y, w, value, label, color = NAVY) {
  slide.addText(value, {
    x, y, w, h: 0.62, margin: 0,
    fontFace: FONT, fontSize: 34, bold: true, color, valign: "bottom",
  });
  slide.addText(label, {
    x, y: y + 0.64, w, h: 0.5, margin: 0,
    fontFace: FONT, fontSize: 10.5, color: MUTED, valign: "top",
  });
}

/* ════════════════════════════ 1 — TITLE ════════════════════════════ */
{
  const s = pres.addSlide();
  s.background = { color: CANVAS };
  n += 1;

  s.addImage({ path: LOGO, x: M, y: 2.35, w: 1.15, h: 1.15 });

  s.addText("TaxIQ", {
    x: M, y: 3.58, w: 8, h: 1.1, margin: 0,
    fontFace: FONT, fontSize: 60, bold: true, color: INK, valign: "middle",
  });
  s.addText("Pakistan's Tax Code. Answered Instantly.", {
    x: M, y: 4.6, w: 9, h: 0.4, margin: 0,
    fontFace: FONT, fontSize: 19, color: NAVY, valign: "middle",
  });
  s.addText(
    "A self-correcting RAG assistant for Pakistani tax professionals — grounded answers with the exact\nstatutory section they came from.",
    { x: M, y: 5.08, w: 9, h: 0.7, margin: 0, fontFace: FONT, fontSize: 12.5, color: MUTED, lineSpacing: 18 },
  );

  // Quiet spec strip, right side
  const specs = [["88,107", "chunks indexed"], ["4", "routing paths"], ["135", "tests, zero network"]];
  specs.forEach((sp, i) => {
    const x = 9.6, y = 2.5 + i * 1.05;
    s.addText(sp[0], { x, y, w: 3.1, h: 0.42, margin: 0, fontFace: FONT, fontSize: 22, bold: true, color: NAVY, valign: "middle" });
    s.addText(sp[1], { x, y: y + 0.4, w: 3.1, h: 0.3, margin: 0, fontFace: FONT, fontSize: 10.5, color: MUTED, valign: "middle" });
  });

  s.addText(String(n), {
    x: W - M - 0.6, y: H - 0.63, w: 0.6, h: 0.26, margin: 0,
    fontFace: FONT, fontSize: 10, color: FAINT, align: "right", valign: "middle",
  });
  s.addNotes("TaxIQ: a retrieval-augmented assistant for Pakistani tax law. Every answer is grounded in a cited statutory section, or the system says it does not know.");
}

/* ════════════════════════════ 2 — OVERVIEW ════════════════════════════ */
{
  const s = frame(pres.addSlide());
  title(s, "What TaxIQ does", "Grounded tax answers, with the section they came from");

  card(s, M, 1.65, 6.15, 4.35);
  s.addText("The problem", {
    x: M + 0.35, y: 1.9, w: 5.5, h: 0.3, margin: 0,
    fontFace: FONT, fontSize: 15, bold: true, color: INK,
  });
  s.addText(
    "Pakistani tax law lives across the Income Tax Ordinance, the Sales Tax Act, annual Finance Acts, " +
    "SROs, circulars and four provincial regimes. Answering one client question means reconciling several " +
    "of them — and a general-purpose chatbot will happily invent a section number that does not exist.",
    { x: M + 0.35, y: 2.3, w: 5.5, h: 1.35, margin: 0, fontFace: FONT, fontSize: 12, color: BODY, lineSpacing: 18 },
  );

  s.addText("The approach", {
    x: M + 0.35, y: 3.75, w: 5.5, h: 0.3, margin: 0,
    fontFace: FONT, fontSize: 15, bold: true, color: INK,
  });
  s.addText(
    [
      { text: "Retrieve first, answer second. ", options: { bold: true, color: NAVY } },
      { text: "The model may only use evidence the system actually retrieved — and it grades that evidence before writing. If it is not sufficient, TaxIQ retries with a better query, then falls back to a grounded web search, and only then says it does not know.", options: { color: BODY } },
    ],
    { x: M + 0.35, y: 4.15, w: 5.5, h: 1.5, margin: 0, fontFace: FONT, fontSize: 12, lineSpacing: 18 },
  );

  // Right: who it's for
  card(s, 7.15, 1.65, W - M - 7.15, 4.35, CARD2);
  s.addText("Who it is for", {
    x: 7.5, y: 1.9, w: 5, h: 0.3, margin: 0,
    fontFace: FONT, fontSize: 15, bold: true, color: INK,
  });

  const who = [
    ["A", "Accounting firms", "Client questions answered with a citable section"],
    ["F", "In-house finance teams", "Withholding rates, filing deadlines, penalties"],
    ["C", "Chartered Accountants", "Cross-checking a position before advising"],
    ["S", "Solo consultants", "The research team they do not have"],
  ];
  who.forEach((w2, i) => {
    const y = 2.4 + i * 0.85;
    badge(s, 7.5, y, w2[0]);
    s.addText(w2[1], { x: 8.0, y: y - 0.04, w: 4.6, h: 0.26, margin: 0, fontFace: FONT, fontSize: 12, bold: true, color: INK });
    s.addText(w2[2], { x: 8.0, y: y + 0.2, w: 4.6, h: 0.26, margin: 0, fontFace: FONT, fontSize: 10.5, color: MUTED });
  });

  s.addText("Answers in Urdu or English · never fabricates a citation", {
    x: M, y: 6.2, w: 11, h: 0.3, margin: 0, fontFace: FONT, fontSize: 11, italic: true, color: NAVY,
  });
  s.addNotes("The product exists because generalist chatbots hallucinate section numbers, which is worse than useless on a compliance product.");
}

/* ════════════════════════ 3 — DATA SOURCES ════════════════════════ */
{
  const s = frame(pres.addSlide());
  title(s, "Where the data comes from", "Primary legislation, collected deliberately — not crawled at random");

  // Three source lanes
  const lanes = [
    {
      x: M, tag: "P1", head: "Primary legislation",
      items: ["Income Tax Ordinance 2001 (full)", "Sales Tax Act 1990 (full)", "Finance Act 2023–24", "Federal Excise Act 2005"],
      note: "Downloaded directly from download1.fbr.gov.pk",
    },
    {
      x: M + 4.15, tag: "P2", head: "Rates & rulings",
      items: ["WHT Rate Card 2024–25", "FBR SROs and circulars", "tax_rates table (curated)"],
      note: "Rate card is also loaded into a structured table for exact lookups",
    },
    {
      x: M + 8.3, tag: "P3", head: "Provincial regimes",
      items: ["PRA (Punjab)", "SRB (Sindh)", "KPKRA (KP)", "Balochistan"],
      note: "Thin coverage — the WEB route fills the gap live",
    },
  ];

  lanes.forEach((L) => {
    card(s, L.x, 1.7, 3.85, 3.35);
    badge(s, L.x + 0.28, 1.95, L.tag, 0.42);
    s.addText(L.head, {
      x: L.x + 0.8, y: 1.97, w: 2.9, h: 0.38, margin: 0,
      fontFace: FONT, fontSize: 13.5, bold: true, color: INK, valign: "middle",
    });
    s.addText(
      L.items.map((t, i) => ({ text: t, options: { bullet: true, breakLine: i < L.items.length - 1 } })),
      { x: L.x + 0.3, y: 2.5, w: 3.3, h: 1.5, margin: 0, fontFace: FONT, fontSize: 11, color: BODY, paraSpaceAfter: 6 },
    );
    s.addText(L.note, {
      x: L.x + 0.3, y: 4.2, w: 3.3, h: 0.7, margin: 0,
      fontFace: FONT, fontSize: 10, italic: true, color: MUTED, lineSpacing: 14,
    });
  });

  // Collection method strip
  card(s, M, 5.25, W - 2 * M, 1.0, WELL);
  s.addText("How it is collected", {
    x: M + 0.3, y: 5.4, w: 2.4, h: 0.28, margin: 0, fontFace: FONT, fontSize: 12, bold: true, color: NAVY,
  });
  s.addText(
    [
      { text: "Scripted download  ", options: { bold: true, color: INK } },
      { text: "scripts/ingest_fbr_docs.py fetches a curated list of FBR PDFs by URL, then chunks and embeds them.    ", options: { color: BODY } },
      { text: "Admin upload  ", options: { bold: true, color: INK } },
      { text: "any document, dropped into the admin panel.", options: { color: BODY } },
    ],
    { x: M + 0.3, y: 5.68, w: 11.5, h: 0.45, margin: 0, fontFace: FONT, fontSize: 11, lineSpacing: 15 },
  );

  s.addText("Cadence: on demand — statutes change annually with the Finance Act, so there is no scheduled crawler. Live/volatile questions are served by the WEB route instead.", {
    x: M, y: 6.45, w: 12.1, h: 0.3, margin: 0, fontFace: FONT, fontSize: 10.5, italic: true, color: AMBER,
  });
  s.addNotes("Deliberately not a web crawler. The corpus is primary legislation fetched by URL. Anything time-sensitive is answered by the live WEB route rather than by re-scraping.");
}

/* ════════════════════════ 4 — PIPELINE ════════════════════════ */
{
  const s = frame(pres.addSlide());
  title(s, "The pipeline", "Ingestion once · retrieval and answer generation per query");

  // ── Lane A: ingestion (top) ──
  s.addText("INGESTION  (once per document)", {
    x: M, y: 1.5, w: 5, h: 0.25, margin: 0, fontFace: FONT, fontSize: 9.5, bold: true, color: FAINT,
  });

  const iy = 1.82, ih = 0.72;
  const iw = 1.95, gap = 0.42;
  const ing = [
    ["Source", "PDF · DOCX · XLSX\nHTML · image"],
    ["Load", "per-format loader\nvision OCR fallback"],
    ["Chunk", "512 chars\n64 overlap"],
    ["Embed", "384-dim vector"],
    ["Store", "document_chunks\n+ tsvector"],
  ];
  ing.forEach((c, i) => {
    const x = M + i * (iw + gap);
    node(s, x, iy, iw, ih, c[0], c[1], { small: true, accent: i === 4 });
    if (i < ing.length - 1) arrow(s, x + iw + 0.06, iy + ih / 2, gap - 0.12, 0);
  });

  // Divider (whitespace + faint rule, no accent stripe)
  s.addShape(pres.ShapeType.line, { x: M, y: 2.95, w: W - 2 * M, h: 0, line: { color: BORDER, width: 1 } });

  // ── Lane B: query ──
  s.addText("PER QUERY", {
    x: M, y: 3.03, w: 5, h: 0.22, margin: 0, fontFace: FONT, fontSize: 9.5, bold: true, color: FAINT,
  });

  // Rewrite → Route
  const qw = 1.5, qh = 0.6;
  node(s, M, 3.72, qw, qh, "Rewrite", "standalone", { small: true });
  arrow(s, M + qw + 0.05, 4.02, 0.3, 0);
  node(s, M + qw + 0.35, 3.72, qw, qh, "Route", "4 paths", { small: true, accent: true });

  // Four route chips, stacked
  const bx = M + 2 * qw + 0.75;                  // 4.37
  const chips = [["DIRECT", 3.32], ["RAG", 3.98], ["WEB", 4.64], ["SQL", 5.30]];
  chips.forEach((c) => {
    const accent = c[0] === "RAG";
    s.addShape(pres.ShapeType.roundRect, {
      x: bx, y: c[1], w: 1.25, h: 0.5, rectRadius: 0.05,
      fill: { color: accent ? NAVY : CARD }, line: { color: accent ? NAVY : BORDER, width: 1 }, shadow: shadow(),
    });
    s.addText(c[0], {
      x: bx, y: c[1], w: 1.25, h: 0.5, margin: 0,
      fontFace: FONT, fontSize: 10.5, bold: true, color: accent ? "FFFFFF" : INK,
      align: "center", valign: "middle",
    });
    arrow(s, M + 2 * qw + 0.4, 4.02, 0.3, c[1] + 0.25 - 4.02);
  });

  // Side notes for the non-RAG routes
  s.addText("answers without retrieval", { x: bx + 1.32, y: 3.36, w: 1.9, h: 0.22, margin: 0, fontFace: FONT, fontSize: 8.5, color: MUTED });
  s.addText("Tavily / Gemini search", { x: bx + 1.32, y: 4.68, w: 1.9, h: 0.22, margin: 0, fontFace: FONT, fontSize: 8.5, color: MUTED });
  s.addText("MCP tool > tax_rates", { x: bx + 1.32, y: 5.34, w: 1.9, h: 0.22, margin: 0, fontFace: FONT, fontSize: 8.5, color: MUTED });

  // RAG detail chain (aligned to the RAG chip)
  const rx = 7.30, cw2 = 1.24, cg = 0.16, cy = 3.88;
  const chain = [["Expand", "n=2"], ["Search", "vector+FTS"], ["RRF", "k = 60"], ["Evaluate", "enough?"]];
  chain.forEach((c, i) => {
    const x = rx + i * (cw2 + cg);
    node(s, x, cy, cw2, 0.7, c[0], c[1], { small: true });
    if (i < chain.length - 1) arrow(s, x + cw2 + 0.01, cy + 0.35, cg - 0.02, 0);
  });
  arrow(s, bx + 1.27, 4.23, rx - bx - 1.29, 0);   // RAG chip → chain

  // Retry loop under the chain
  const lastX = rx + 3 * (cw2 + cg) + cw2 / 2;
  s.addShape(pres.ShapeType.line, { x: lastX, y: 4.58, w: 0, h: 0.42, line: { color: AMBER, width: 1.2, dashType: "dash" } });
  s.addShape(pres.ShapeType.line, { x: rx + cw2 / 2, y: 5.0, w: lastX - rx - cw2 / 2, h: 0, line: { color: AMBER, width: 1.2, dashType: "dash" } });
  arrow(s, rx + cw2 / 2, 5.0, 0, -0.42, { color: AMBER, dash: true });
  s.addText("retry with evaluator feedback", {
    x: rx + 0.15, y: 5.02, w: 3.4, h: 0.24, margin: 0, fontFace: FONT, fontSize: 8.5, italic: true, color: AMBER,
  });

  // ── Lane C: output ──
  s.addShape(pres.ShapeType.line, { x: M, y: 5.62, w: W - 2 * M, h: 0, line: { color: BORDER, width: 1 } });
  const oy = 5.78, oh = 0.66;
  node(s, M, oy, 3.5, oh, "Grounded answer", "streamed, cited by section", { accent: true });
  arrow(s, M + 3.55, oy + 0.33, 0.3, 0);
  node(s, M + 3.9, oy, 2.7, oh, "File (optional)", "PDF · XLSX · DOCX", { small: true });
  arrow(s, M + 6.65, oy + 0.33, 0.3, 0);
  node(s, M + 7.0, oy, 2.9, oh, "Persist + trace", "runs · steps · messages", { small: true });
  s.addText("Every step streams live over SSE\nand is replayable afterwards.", {
    x: M + 10.1, y: oy - 0.02, w: 2.0, h: 0.7, margin: 0,
    fontFace: FONT, fontSize: 8.5, italic: true, color: MUTED, valign: "middle", lineSpacing: 11,
  });
  s.addNotes("Two lanes: ingestion happens once per document; the query lane runs per message. The retry loop is what makes it self-correcting — the evaluator's feedback rewrites the query rather than repeating it.");
}

/* ════════════════════════ 5 — DATABASE ════════════════════════ */
{
  const s = frame(pres.addSlide());
  title(s, "Database structure", "PostgreSQL — application data and pipeline traces in one schema");

  const box = (x, y, w, h, name, cols, accent) => {
    slideBox(s, x, y, w, h, name, cols, accent);
  };
  function slideBox(sl, x, y, w, h, name, cols, accent) {
    sl.addShape(pres.ShapeType.roundRect, {
      x, y, w, h, rectRadius: 0.05,
      fill: { color: accent ? NAVY : CARD }, line: { color: accent ? NAVY : BORDER, width: 1 }, shadow: shadow(),
    });
    sl.addText(name, {
      x, y: y + 0.05, w, h: 0.28, margin: 0,
      fontFace: FONT, fontSize: 11, bold: true, color: accent ? "FFFFFF" : INK, align: "center", valign: "middle",
    });
    sl.addText(cols, {
      x: x + 0.12, y: y + 0.32, w: w - 0.24, h: h - 0.38, margin: 0,
      fontFace: FONT, fontSize: 8.5, color: accent ? "CADCFC" : MUTED, align: "center", lineSpacing: 11,
    });
  }

  // Column 1 — identity
  box(M, 1.75, 2.3, 0.95, "users", "auth · is_admin\nplan", true);
  box(M, 3.05, 2.3, 0.95, "projects", "domain_context\nproject_memory");
  box(M, 4.35, 2.3, 0.95, "user_context_profiles", "language · llm_mode\ntax context");

  // Column 2 — conversation
  box(3.35, 1.75, 2.5, 0.95, "sessions", "one conversation\nowner-scoped", true);
  box(3.35, 3.05, 2.5, 0.95, "messages", "user + assistant\ncitations");
  box(3.35, 4.35, 2.5, 0.95, "session_attachments", "per-chat files\nnever embedded");
  box(3.35, 5.65, 2.5, 0.8, "generated_files", "PDF · XLSX · DOCX");

  // Column 3 — observability
  box(6.9, 1.75, 2.4, 0.95, "pipeline_runs", "route · outcome\nduration", true);
  box(6.9, 3.05, 2.4, 0.95, "pipeline_steps", "per-step timing\nstatus");
  box(6.9, 4.35, 2.4, 0.95, "error_logs", "severity · module\nstack trace");
  box(6.9, 5.65, 2.4, 0.8, "mcp_tool_calls", "SQL route audit");

  // Column 4 — knowledge
  box(10.35, 1.75, 2.35, 0.95, "documents", "filename · type\nis_global", true);
  box(10.35, 3.05, 2.35, 1.15, "document_chunks", "vector(384)\ntsvector · source_file");
  box(10.35, 4.55, 2.35, 0.95, "tax_rates", "structured rates\nSQL route");
  box(10.35, 5.7, 2.35, 0.75, "ingestion_jobs", "upload status");

  // Relationship lines
  const rel = (x1, y1, x2, y2) => s.addShape(pres.ShapeType.line, {
    x: x1, y: y1, w: x2 - x1, h: y2 - y1, line: { color: NAVY_L, width: 1 },
  });
  rel(2.9, 2.22, 3.35, 2.22);          // users → sessions
  rel(2.9, 3.52, 3.35, 2.6);           // projects → sessions
  rel(4.6, 2.7, 4.6, 3.05);            // sessions → messages
  rel(4.6, 4.0, 4.6, 4.35);            // messages → attachments
  rel(4.6, 5.3, 4.6, 5.65);            // → generated_files
  rel(5.85, 2.22, 6.9, 2.22);          // sessions → runs
  rel(8.1, 2.7, 8.1, 3.05);            // runs → steps
  rel(8.1, 4.0, 8.1, 4.35);            // steps → errors
  rel(8.1, 5.3, 8.1, 5.65);            // → mcp calls
  rel(11.5, 2.7, 11.5, 3.05);          // documents → chunks

  // Retrieval callout
  s.addText("↑ hybrid retrieval reads here", {
    x: 9.55, y: 3.35, w: 0.75, h: 0.5, margin: 0,
    fontFace: FONT, fontSize: 8.5, italic: true, color: NAVY, align: "right",
  });

  s.addText("Chat attachments live in their own table and are never written to document_chunks — the knowledge base and per-conversation files cannot leak into each other.", {
    x: M, y: 6.65, w: 12.1, h: 0.3, margin: 0, fontFace: FONT, fontSize: 10.5, italic: true, color: NAVY,
  });
  s.addNotes("Four groups: identity, conversation, observability, knowledge. The separation between session_attachments and document_chunks is structural, not a filter.");
}

/* ════════════════════════ 6 — FEATURES ════════════════════════ */
{
  const s = frame(pres.addSlide());
  title(s, "Features", "Grouped by who touches them");

  const groups = [
    {
      x: M, head: "For the tax professional", tag: "1",
      items: [
        ["Grounded chat", "Cited answers, streamed live, in Urdu or English"],
        ["Reasoning trail", "See what it searched; expand it after the fact"],
        ["Attach a file", "Add a payslip or ledger to one conversation"],
        ["Export", "Answer as PDF, Excel or Word — or export the whole chat"],
      ],
    },
    {
      x: M + 4.15, head: "For the firm", tag: "2",
      items: [
        ["Projects", "Scope chats to a client, with injected context"],
        ["Project memory", "Facts from earlier chats carry forward"],
        ["Personalization", "Saved situation + language, applied to every path"],
        ["Session history", "Rename, revisit, soft-delete"],
      ],
    },
    {
      x: M + 8.3, head: "For the administrator", tag: "3",
      items: [
        ["Knowledge base", "Upload documents into the shared corpus"],
        ["Ingestion status", "processing / success / failed, with the reason"],
        ["Analytics", "Traffic, routing, latency, errors"],
        ["Audit", "Every run, step and MCP call, replayable"],
      ],
    },
  ];

  groups.forEach((g) => {
    card(s, g.x, 1.7, 3.85, 4.5);
    badge(s, g.x + 0.28, 1.95, g.tag, 0.42);
    s.addText(g.head, {
      x: g.x + 0.8, y: 1.97, w: 2.9, h: 0.38, margin: 0,
      fontFace: FONT, fontSize: 13.5, bold: true, color: INK, valign: "middle",
    });
    g.items.forEach((it, i) => {
      const y = 2.6 + i * 0.87;
      s.addText(it[0], { x: g.x + 0.3, y, w: 3.3, h: 0.26, margin: 0, fontFace: FONT, fontSize: 11.5, bold: true, color: NAVY });
      s.addText(it[1], { x: g.x + 0.3, y: y + 0.25, w: 3.3, h: 0.5, margin: 0, fontFace: FONT, fontSize: 10, color: MUTED, lineSpacing: 13 });
    });
  });

  s.addText("Cross-cutting: JWT + CSRF auth · rate limiting · dual database gateways · live SSE trace on every request", {
    x: M, y: 6.45, w: 12.1, h: 0.3, margin: 0, fontFace: FONT, fontSize: 10.5, italic: true, color: MUTED,
  });
  s.addNotes("Deliberately grouped by audience rather than dumped as one list.");
}

/* ═══════════════════ 7 — ADMIN DASHBOARD ═══════════════════ */
{
  const s = frame(pres.addSlide());
  title(s, "Admin dashboard", "Four questions, answered from real rows — nothing is placeholder");

  // Metric cards
  const metrics = [
    ["Chunks indexed", "88,107", "Is the corpus actually there?", NAVY],
    ["Requests (7d)", "53", "Is anyone using it, and how?", NAVY],
    ["Median latency", "38.7s", "p95 119.6s — the honest number", AMBER],
    ["Errors logged", "0", "Silent failures become visible", GREEN],
  ];
  metrics.forEach((m, i) => {
    const x = M + i * 3.07;
    card(s, x, 1.65, 2.85, 1.5);
    s.addText(m[0], { x: x + 0.22, y: 1.78, w: 2.4, h: 0.25, margin: 0, fontFace: FONT, fontSize: 9.5, bold: true, color: FAINT });
    s.addText(m[1], { x: x + 0.22, y: 2.03, w: 2.4, h: 0.48, margin: 0, fontFace: FONT, fontSize: 24, bold: true, color: m[3] });
    s.addText(m[2], { x: x + 0.22, y: 2.53, w: 2.45, h: 0.45, margin: 0, fontFace: FONT, fontSize: 9, color: MUTED, lineSpacing: 12 });
  });

  // Chart: where the time goes (real per-step latency)
  card(s, M, 3.35, 7.3, 3.0);
  s.addText("Where the time goes", {
    x: M + 0.3, y: 3.5, w: 4.5, h: 0.28, margin: 0, fontFace: FONT, fontSize: 13, bold: true, color: INK,
  });
  s.addText("Average duration per pipeline step — this is the chart that says why, not just that", {
    x: M + 0.3, y: 3.76, w: 6.6, h: 0.25, margin: 0, fontFace: FONT, fontSize: 9.5, color: MUTED,
  });
  s.addChart(pres.ChartType.bar, [{
    name: "Average (s)",
    labels: ["Retrieval", "Response", "Router", "Rewriter", "Evaluator"],
    values: [12.7, 7.8, 7.0, 6.4, 0.4],
  }], {
    x: M + 0.2, y: 4.05, w: 6.9, h: 2.1,
    barDir: "bar", chartColors: [NAVY],
    showValue: true, dataLabelPosition: "outEnd", dataLabelColor: BODY, dataLabelFontSize: 9, dataLabelFontFace: FONT,
    showLegend: false, showTitle: false,
    catAxisLabelColor: BODY, catAxisLabelFontSize: 9.5, catAxisLabelFontFace: FONT,
    valAxisLabelColor: MUTED, valAxisLabelFontSize: 8.5, valAxisLabelFontFace: FONT,
    valGridLine: { color: BORDER, size: 1 }, catGridLine: { style: "none" },
    valAxisMaxVal: 15, barGapWidthPct: 60,
  });

  // Routing donut
  card(s, M + 7.55, 3.35, 4.55, 3.0);
  s.addText("How requests route", {
    x: M + 7.85, y: 3.5, w: 4, h: 0.28, margin: 0, fontFace: FONT, fontSize: 13, bold: true, color: INK,
  });
  s.addChart(pres.ChartType.doughnut, [{
    name: "Routes",
    labels: ["RAG", "SQL", "WEB", "DIRECT"],
    values: [23, 8, 5, 5],
  }], {
    x: M + 7.7, y: 3.75, w: 4.25, h: 2.4,
    chartColors: [NAVY, NAVY_L, GREEN, AMBER],
    holeSize: 55, showLegend: true, legendPos: "b", legendFontSize: 9, legendFontFace: FONT, legendColor: BODY,
    showTitle: false, showValue: false,
  });

  s.addText("Live figures from the development instance. Percentiles are nearest-rank, so a reported p95 is a latency a real request actually experienced.", {
    x: M, y: 6.55, w: 12.1, h: 0.3, margin: 0, fontFace: FONT, fontSize: 9.5, italic: true, color: FAINT,
  });
  s.addNotes("The per-step latency chart is the one that earns its place: total latency tells you that you're slow, per-step tells you retrieval is the reason.");
}

/* ════════════════════════ 8 — TESTING ════════════════════════ */
{
  const s = frame(pres.addSlide());
  title(s, "Testing", "135 tests · ~10 seconds · zero network access");

  // Left: the three rules
  card(s, M, 1.7, 5.35, 4.4);
  s.addText("Three rules", {
    x: M + 0.35, y: 1.95, w: 4.6, h: 0.3, margin: 0, fontFace: FONT, fontSize: 14, bold: true, color: INK,
  });
  const rules = [
    ["No network, ever", "An autouse fixture blocks every non-loopback socket. A missed mock fails loudly instead of silently hitting production Supabase or a paid LLM API."],
    ["Guard behaviour, not implementation", "Most tests exist because that behaviour actually broke. Each carries a note naming the bug it protects against."],
    ["Failures must be loud", "Several tests assert that an error surfaces. Silently swallowed failures were this codebase's most damaging bug class."],
  ];
  rules.forEach((r, i) => {
    const y = 2.45 + i * 1.2;
    badge(s, M + 0.35, y, String(i + 1), 0.34);
    s.addText(r[0], { x: M + 0.82, y: y - 0.02, w: 4.2, h: 0.26, margin: 0, fontFace: FONT, fontSize: 12, bold: true, color: NAVY });
    s.addText(r[1], { x: M + 0.82, y: y + 0.24, w: 4.2, h: 0.8, margin: 0, fontFace: FONT, fontSize: 9.5, color: MUTED, lineSpacing: 12 });
  });

  // Right: coverage table
  card(s, 6.5, 1.7, W - M - 6.5, 4.4, CARD2);
  s.addText("What is covered", {
    x: 6.85, y: 1.95, w: 4, h: 0.3, margin: 0, fontFace: FONT, fontSize: 14, bold: true, color: INK,
  });
  s.addTable(
    [
      [
        { text: "Suite", options: { bold: true, color: FAINT, fontSize: 9 } },
        { text: "Guards", options: { bold: true, color: FAINT, fontSize: 9 } },
      ],
      ["Persistence", "Session ownership, message round-trip, backend parity"],
      ["Orchestrator", "Full pipeline with fakes; settings reach every path"],
      ["Attachments", "Knowledge-base separation cannot be broken"],
      ["Pipeline", "Rewriter/router/evaluator vs. messy LLM output"],
      ["File generation", "Ragged tables, XML-special chars, valid output"],
      ["Analytics", "Percentiles, bucketing, routing shares"],
      ["API", "Auth required; cross-user access denied"],
      ["Retrieval", "RRF, BM25, chunking, token budget"],
    ],
    {
      x: 6.85, y: 2.4, w: 5.85, colW: [1.75, 4.1],
      fontFace: FONT, fontSize: 9.5, color: BODY,
      border: { type: "solid", color: BORDER, pt: 0.5 },
      fill: { color: "FFFFFF" }, rowH: 0.38, valign: "middle", autoPage: false,
    },
  );

  s.addText("Verified by mutation: two original bugs were reintroduced, and exactly the two tests written for them failed.  ·  CI runs the suite and both frontend builds on every push.", {
    x: M, y: 6.4, w: 12.1, h: 0.35, margin: 0, fontFace: FONT, fontSize: 10.5, italic: true, color: NAVY, lineSpacing: 14,
  });
  s.addNotes("Coverage is by behaviour, not line percentage. The suite was mutation-checked: reintroducing a real bug turns exactly the right test red.");
}

/* ════════════════════════ 9 — TECH STACK ════════════════════════ */
{
  const s = frame(pres.addSlide());
  title(s, "Technology", "Boring where it can be, deliberate where it matters");

  const cols = [
    { head: "Backend", items: ["Python 3.11", "FastAPI + Uvicorn", "SQLAlchemy (async)", "asyncpg", "Pydantic"] },
    { head: "Data", items: ["PostgreSQL", "pgvector (384-dim)", "tsvector full-text", "Supabase (hosted)", "rank-bm25"] },
    { head: "AI", items: ["Groq — LLaMA 3.3 70B", "Gemini 2.5 Flash", "Gemini Vision (OCR)", "Tavily (web)", "MCP (SQL tool)"] },
    { head: "Frontend", items: ["React 19 + TypeScript", "Vite", "Tailwind CSS", "Zustand", "Recharts (admin)"] },
  ];

  cols.forEach((c, i) => {
    const x = M + i * 3.07;
    card(s, x, 1.7, 2.85, 3.4);
    s.addText(c.head, {
      x: x + 0.25, y: 1.9, w: 2.4, h: 0.3, margin: 0, fontFace: FONT, fontSize: 13, bold: true, color: NAVY,
    });
    c.items.forEach((it, j) => {
      const y = 2.35 + j * 0.5;
      s.addShape(pres.ShapeType.ellipse, { x: x + 0.27, y: y + 0.09, w: 0.09, h: 0.09, fill: { color: NAVY_L }, line: { color: NAVY_L, width: 0 } });
      s.addText(it, { x: x + 0.48, y, w: 2.3, h: 0.3, margin: 0, fontFace: FONT, fontSize: 10.5, color: BODY, valign: "middle" });
    });
  });

  // Two deliberate choices
  card(s, M, 5.3, 5.95, 1.2, WELL);
  s.addText("Search stays inside Postgres", {
    x: M + 0.28, y: 5.45, w: 5.4, h: 0.26, margin: 0, fontFace: FONT, fontSize: 11.5, bold: true, color: INK,
  });
  s.addText("pgvector + tsvector in one query. No separate vector database to keep in sync.", {
    x: M + 0.28, y: 5.72, w: 5.4, h: 0.6, margin: 0, fontFace: FONT, fontSize: 10, color: MUTED, lineSpacing: 13,
  });

  card(s, 6.85, 5.3, 5.85, 1.2, WELL);
  s.addText("Two database gateways", {
    x: 7.13, y: 5.45, w: 5.3, h: 0.26, margin: 0, fontFace: FONT, fontSize: 11.5, bold: true, color: INK,
  });
  s.addText("Direct asyncpg at the office; Supabase REST on IPv4-only networks. It fails over automatically.", {
    x: 7.13, y: 5.72, w: 5.3, h: 0.6, margin: 0, fontFace: FONT, fontSize: 10, color: MUTED, lineSpacing: 13,
  });
  s.addNotes("Both callouts are choices, not accidents: one vector store instead of two systems, and a REST fallback because the Postgres wire protocol is blocked on some ISPs.");
}

/* ════════════════════════ 10 — VERSION HISTORY ════════════════════════ */
{
  const s = frame(pres.addSlide());
  title(s, "How it got here", "Five iterations — each one closed a gap the last one exposed");

  const versions = [
    ["v1", "The RAG core", ["Rewriter → router → retrieve → evaluate", "ChromaDB + BM25, RRF fusion", "SSE pipeline trace", "SQLite step logging"]],
    ["v2", "Real infrastructure", ["Postgres + pgvector, Supabase", "JWT + CSRF authentication", "4-way router: DIRECT/RAG/WEB/SQL", "MCP tax_rates · file generation"]],
    ["v3", "Resilience", ["Dual gateways: direct + REST", "API key rotation", "Query expansion", "Admin app split out"]],
    ["v4", "Client workflows", ["Projects + rolling memory", "Chat export", "Per-user personalization", "Attachment groundwork"]],
    ["Now", "Hardening", ["Full audit: 7 issue areas fixed", "135-test suite, CI", "Design system + logo", "Analytics dashboard, ingestion moved to admin"]],
  ];

  const cw = 2.24, cx0 = M;
  versions.forEach((v, i) => {
    const x = cx0 + i * (cw + 0.15);
    const current = i === versions.length - 1;
    card(s, x, 2.1, cw, 3.9, current ? CARD : CARD2);

    // Version chip
    s.addShape(pres.ShapeType.roundRect, {
      x: x + 0.18, y: 1.82, w: current ? 0.82 : 0.62, h: 0.36, rectRadius: 0.06,
      fill: { color: current ? NAVY : WELL }, line: { color: current ? NAVY : BORDER, width: 1 },
    });
    s.addText(v[0], {
      x: x + 0.18, y: 1.82, w: current ? 0.82 : 0.62, h: 0.36, margin: 0,
      fontFace: FONT, fontSize: 11, bold: true, color: current ? "FFFFFF" : NAVY, align: "center", valign: "middle",
    });

    s.addText(v[1], {
      x: x + 0.2, y: 2.4, w: cw - 0.4, h: 0.3, margin: 0,
      fontFace: FONT, fontSize: 12, bold: true, color: INK,
    });
    s.addText(
      v[2].map((t, j) => ({ text: t, options: { bullet: true, breakLine: j < v[2].length - 1 } })),
      { x: x + 0.2, y: 2.8, w: cw - 0.4, h: 2.9, margin: 0, fontFace: FONT, fontSize: 9.5, color: BODY, paraSpaceAfter: 7, lineSpacing: 12 },
    );
  });

  // Timeline rule behind the chips
  s.addShape(pres.ShapeType.line, {
    x: M + 0.5, y: 2.0, w: 11.6, h: 0, line: { color: BORDER, width: 1 },
  });

  s.addText("The current pass was a full audit, not a feature release: chat history did not persist, file generation failed silently, and every answer was being served by a 2B model through a dead tunnel.", {
    x: M, y: 6.25, w: 12.1, h: 0.5, margin: 0, fontFace: FONT, fontSize: 10.5, italic: true, color: MUTED, lineSpacing: 14,
  });
  s.addNotes("v1-v4 added capability; the current pass fixed the foundation those versions had quietly broken.");
}

/* ════════════════════════ 11 — CLOSING ════════════════════════ */
{
  const s = pres.addSlide();
  n += 1;
  s.background = { color: NAVY_D };

  s.addImage({ path: LOGO, x: M, y: 1.5, w: 0.8, h: 0.8 });

  s.addText("Where it stands", {
    x: M, y: 2.5, w: 6.6, h: 0.6, margin: 0,
    fontFace: FONT, fontSize: 34, bold: true, color: "FFFFFF", valign: "middle",
  });
  s.addText(
    "The product works end to end: grounded answers with citations, a self-correcting retry loop, " +
    "in-chat file generation, and an admin dashboard reporting live figures. It is tested, and the " +
    "known gaps are written down rather than discovered later.",
    { x: M, y: 3.2, w: 6.6, h: 1.4, margin: 0, fontFace: FONT, fontSize: 13, color: "CADCFC", lineSpacing: 20 },
  );

  // Next steps
  const next = [
    ["Next", "Wire in the citation validator · re-embed the corpus with a stronger model · responsive layout"],
    ["Then", "Clickable citations to the exact chunk · in-chat tax calculator · conflict checks across sources"],
    ["Not planned", "A separate vector database — keeping search inside Postgres is the point"],
  ];
  next.forEach((it, i) => {
    const y = 2.55 + i * 1.25;
    s.addShape(pres.ShapeType.roundRect, {
      x: 7.6, y, w: 5.1, h: 1.0, rectRadius: 0.06,
      fill: { color: "1E3560" }, line: { color: "3A5686", width: 1 },
    });
    s.addText(it[0], {
      x: 7.85, y: y + 0.1, w: 4.6, h: 0.26, margin: 0,
      fontFace: FONT, fontSize: 10, bold: true, color: NAVY_L,
    });
    s.addText(it[1], {
      x: 7.85, y: y + 0.35, w: 4.6, h: 0.6, margin: 0,
      fontFace: FONT, fontSize: 10.5, color: "FFFFFF", lineSpacing: 14,
    });
  });

  s.addText("Accuracy over speed. Transparency over black-box magic.", {
    x: M, y: 6.0, w: 8, h: 0.4, margin: 0,
    fontFace: FONT, fontSize: 14, italic: true, color: NAVY_L, valign: "middle",
  });

  s.addText(String(n), {
    x: W - M - 0.6, y: H - 0.63, w: 0.6, h: 0.26, margin: 0,
    fontFace: FONT, fontSize: 10, color: NAVY_L, align: "right", valign: "middle",
  });
  s.addNotes("Close on the honest position: it works, it is tested, and the remaining gaps are documented.");
}

pres.writeFile({ fileName: "TaxIQ.pptx" }).then(() => console.log("TaxIQ.pptx written —", n, "slides"));
