# ============================================================
# Generate docs/database-schema.svg — the real Muhafiz relational-schema
# ERD, from docs/schema-snapshot.json (produced by
# scripts/generate_schema_snapshot.py, which introspects the live
# database directly — this script never talks to Postgres itself).
#
#   python scripts/generate_schema_snapshot.py   # refresh the JSON first
#   python scripts/build_erd.py
#
# REPLACES A LEFTOVER ARTIFACT FROM AN UNRELATED PRODUCT. The version of
# this file that shipped here before 2026-08-26 was not a Muhafiz schema
# renderer at all — its own title ("TaxIQ — Database Schema") and
# hardcoded table layout (tax_rates, document_chunks/pgvector — none of
# which exist in this schema) confirm it was a copy-pasted/leftover
# artifact from a different product, silently rendering a wrong diagram
# under docs/database-schema.png this whole time (caught when a user
# looked at the actual rendered image, not by code review — see
# DOCUMENTATION_GAPS_FIX_PROMPT.md Module D for the audit trail and the
# original, narrower decision to fix only the JSON snapshot; this script
# is the follow-up that fixes the image too).
#
# AUTO-LAYOUT, NOT HAND-TUNED. The old TaxIQ script hardcoded pixel
# positions for its own fixed 16-table set — brittle by construction for
# a schema whose real table count has already grown from 16 (this
# product's own earliest form) to 27 and will keep growing. This script
# instead: (1) buckets every table into a ZONE by name, mirroring
# docs/DATABASE_DESIGN.md's own section headers exactly, so the two stay
# readable side by side; (2) lays out tables within a zone as a wrapping
# grid; (3) shelf-packs zones onto the page left-to-right, top-to-bottom.
# A table schema-snapshot.json doesn't know about yet renders as an
# "UNZONED" catch-all rather than silently vanishing from the diagram.
#
# SVG IS THE PRIMARY OUTPUT; PNG IS OPTIONAL, GENERATED SEPARATELY. This
# script only writes docs/database-schema.svg — README.md embeds that
# directly (GitHub and every browser render inline SVG natively), so a
# PNG isn't required to view the diagram. If a rasterized .png is still
# wanted (e.g. for a context that doesn't render SVG), no system-level
# SVG tool (cairosvg, rsvg-convert, Inkscape, ImageMagick) was available
# in this environment, but a pure-pip path was: `pip install svglib
# reportlab rlPyCairo`, then
#     from svglib.svglib import svg2rlg
#     from reportlab.graphics import renderPM
#     renderPM.drawToFile(svg2rlg("docs/database-schema.svg"),
#                          "docs/database-schema.png", fmt="PNG")
# Not added to requirements.txt — this is one-off docs tooling, not an
# app runtime dependency.
# ============================================================
import json
from collections import OrderedDict
from xml.sax.saxutils import escape as _esc

d = json.load(open("docs/schema-snapshot.json", encoding="utf-8"))
FK = d["_fks"]

# ── design tokens (product identity — same palette Muhafiz's own frontend
#    token set uses, docs/DESIGN.md) ─────────────────────────────────────
NAVY = "#27477D"; INK = "#1C1B18"; BODY = "#4B4842"; MUTED = "#86827A"
CANVAS = "#F4F2ED"; CARD = "#FFFFFF"; BORDER = "#D5D0C4"
G_ID = "#27477D"     # identity & access — navy
G_CASE = "#3F7D58"   # cases & evidence — green
G_CONV = "#B0762A"   # conversation & pipeline — amber
G_REF = "#8B4F9F"    # reference data — purple
G_COMM = "#1F8A8C"   # community detection — teal
G_SCALE = "#B85C38"  # scale prerequisites — rust
G_QUAL = "#5C6BC0"   # ingestion quality — indigo
G_MIG = "#86827A"    # migration tracking — muted grey

# ── zones, mirroring docs/DATABASE_DESIGN.md's own section headers ──────
ZONES: "OrderedDict[str, tuple[str, list[str]]]" = OrderedDict([
    ("IDENTITY & ACCESS", (G_ID, [
        "users", "user_context_profiles", "case_assignments", "audit_logs",
    ])),
    ("CASES & EVIDENCE", (G_CASE, [
        "cases", "documents", "ingestion_jobs", "session_attachments",
    ])),
    ("REFERENCE DATA", (G_REF, [
        "police_reference_data",
    ])),
    ("CONVERSATION & PIPELINE", (G_CONV, [
        "sessions", "messages", "generated_files", "projects",
        "project_memory", "pipeline_runs", "pipeline_steps",
        "mcp_tool_calls", "error_logs",
    ])),
    ("COMMUNITY DETECTION", (G_COMM, [
        "community_runs", "community_membership", "community_reports",
    ])),
    ("SCALE PREREQUISITES", (G_SCALE, [
        "identity_index", "chunk_fulltext", "pending_candidate_priority",
    ])),
    ("INGESTION QUALITY", (G_QUAL, [
        "ingestion_run_quality", "entity_resolution_consistency_findings",
    ])),
    ("MIGRATION TRACKING", (G_MIG, [
        "alembic_version",
    ])),
])

_zoned = {t for _, (_, tables) in ZONES.items() for t in tables}
_unzoned = sorted(set(d.keys()) - {"_fks"} - _zoned)
if _unzoned:
    # A table schema-snapshot.json knows about that this script's own
    # ZONES dict hasn't been taught yet — render it visibly rather than
    # silently drop it (the exact failure mode that made the old TaxIQ
    # script's staleness invisible for so long).
    ZONES["UNZONED (add to this script's ZONES dict)"] = (MUTED, _unzoned)

TYPEMAP = {
    'varchar': 'text', 'bpchar': 'char', 'int4': 'int', 'int8': 'bigint',
    'float8': 'float', 'bool': 'bool', 'timestamp': 'timestamp',
    'timestamptz': 'timestamptz', 'jsonb': 'jsonb', 'uuid': 'uuid',
    'numeric': 'numeric', 'date': 'date', 'tsvector': 'tsvector',
    'text': 'text', '_text': 'text[]',
}

fkcols: dict = {}
for f in FK:
    fkcols.setdefault(f["src"], set()).add(f["src_col"])

# ── per-table box sizing ─────────────────────────────────────────────────
ROWH = 16; HEADH = 26; BOXW = 236; PAD = 6
ZONE_PAD = 18; ZONE_HEADER = 30; ZONE_GAP = 24; ZONE_COLS = 2
CANVAS_TARGET_W = 1900


def box_h(t: str) -> float:
    return HEADH + len(d[t]["columns"]) * ROWH + PAD


def layout_zone_tables(tables: list[str]) -> tuple[dict, float, float]:
    """Wrapping grid within one zone: ZONE_COLS columns, each column's
    own running height (tables are NOT force-aligned into rows — a tall
    table in column 1 doesn't push column 2 down.). Returns
    {table: (x, y)} relative to the zone's own top-left content origin,
    plus the zone's total content (width, height)."""
    col_heights = [0.0] * ZONE_COLS
    positions = {}
    for i, t in enumerate(tables):
        col = min(range(ZONE_COLS), key=lambda c: col_heights[c])
        x = col * (BOXW + ZONE_PAD)
        y = col_heights[col]
        positions[t] = (x, y)
        col_heights[col] = y + box_h(t) + ZONE_PAD
    width = ZONE_COLS * BOXW + (ZONE_COLS - 1) * ZONE_PAD
    height = max(col_heights) if col_heights else 0
    return positions, width, height


def shelf_pack(zone_sizes: list[tuple[str, float, float]]) -> dict:
    """zone_sizes: [(zone_name, width, height)]. Places zones left-to-
    right, wrapping to a new shelf when the running row width would
    exceed CANVAS_TARGET_W. Returns {zone_name: (x, y)}."""
    positions = {}
    x = cursor_y = row_h = 0.0
    for name, w, h in zone_sizes:
        if x > 0 and x + w > CANVAS_TARGET_W:
            cursor_y += row_h + ZONE_GAP
            x = 0
            row_h = 0
        positions[name] = (x, cursor_y)
        x += w + ZONE_GAP
        row_h = max(row_h, h)
    total_h = cursor_y + row_h
    return positions, total_h


# Compute each zone's own content size first (independent of page position).
zone_layouts = {}
zone_sizes = []
for zname, (color, tables) in ZONES.items():
    positions, cw, ch = layout_zone_tables(tables)
    zone_layouts[zname] = positions
    zone_sizes.append((zname, cw + 2 * ZONE_PAD, ch + ZONE_HEADER + ZONE_PAD))

zone_page_pos, PAGE_CONTENT_H = shelf_pack(zone_sizes)

# Resolve every table's ABSOLUTE (x, y) on the page, plus its zone/color.
TABLE_POS: dict = {}
TABLE_ZONE_COLOR: dict = {}
for zname, (color, tables) in ZONES.items():
    zx, zy = zone_page_pos[zname]
    for t in tables:
        rx, ry = zone_layouts[zname][t]
        TABLE_POS[t] = (zx + ZONE_PAD + rx, zy + ZONE_HEADER + ry)
        TABLE_ZONE_COLOR[t] = color

W = CANVAS_TARGET_W + 40
H = int(PAGE_CONTENT_H) + 140
svg = []
svg.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" font-family="Segoe UI, Arial, sans-serif">')
svg.append(f'<rect width="{W}" height="{H}" fill="{CANVAS}"/>')
svg.append('<defs><filter id="sh" x="-20%" y="-20%" width="140%" height="140%">'
            '<feDropShadow dx="0" dy="2" stdDeviation="3" flood-color="#1C1B18" flood-opacity="0.10"/></filter></defs>')

table_count = len(d) - 1
svg.append(f'<text x="20" y="34" font-size="26" font-weight="700" fill="{INK}">Muhafiz &#8212; Database Schema</text>')
svg.append(f'<text x="22" y="56" font-size="13" fill="{MUTED}">PostgreSQL &#183; {table_count} tables &#183; captured from the live database via scripts/generate_schema_snapshot.py</text>')

PAGE_Y0 = 76

# ── zone tint rectangles + labels ────────────────────────────────────────
for zname, (color, tables) in ZONES.items():
    zx, zy = zone_page_pos[zname]
    zw = zone_sizes[[n for n, _, _ in zone_sizes].index(zname)][1]
    zh = zone_sizes[[n for n, _, _ in zone_sizes].index(zname)][2]
    svg.append(f'<rect x="{zx+20}" y="{zy+PAGE_Y0}" width="{zw}" height="{zh}" rx="14" '
                f'fill="{color}" fill-opacity="0.05" stroke="{color}" stroke-opacity="0.28" stroke-width="1.5"/>')
    svg.append(f'<text x="{zx+20+14}" y="{zy+PAGE_Y0+22}" font-size="12" font-weight="700" letter-spacing="1.2" fill="{color}">{_esc(zname)}</text>')


def box(t: str):
    x, y = TABLE_POS[t]
    return x + 20, y + PAGE_Y0, BOXW, box_h(t)


def col_y(t: str, colname: str) -> float:
    x, y, w, h = box(t)
    for i, c in enumerate(d[t]["columns"]):
        if c["name"] == colname:
            return y + HEADH + i * ROWH + ROWH / 2
    return y + HEADH


def edge_x(t: str, side: str) -> float:
    x, y, w, h = box(t)
    return x + w if side == "r" else x


def crow(x, y, dirx, color):
    dx = 10 * (-dirx)
    return (f'<path d="M{x} {y} l{dx} {-5} M{x} {y} l{dx} {5} M{x} {y} l{dx} 0" '
            f'stroke="{color}" stroke-width="1.5" fill="none"/>')


def one_bar(x, y, dirx, color):
    return f'<line x1="{x-4*dirx}" y1="{y-5}" x2="{x-4*dirx}" y2="{y+5}" stroke="{color}" stroke-width="1.5"/>'


# ── relationship lines (drawn before boxes so boxes sit on top) ─────────
for f in FK:
    parent, pcol, child, ccol = f["tgt"], f["tgt_col"], f["src"], f["src_col"]
    if parent not in TABLE_POS or child not in TABLE_POS:
        continue
    color = TABLE_ZONE_COLOR[parent]
    ppos, cpos = TABLE_POS[parent], TABLE_POS[child]
    if cpos[0] >= ppos[0]:
        pside, cside, pdir, cdir = "r", "l", 1, -1
    else:
        pside, cside, pdir, cdir = "l", "r", -1, 1
    px, py = edge_x(parent, pside), col_y(parent, pcol)
    cx, cy = edge_x(child, cside), col_y(child, ccol)
    midx = (px + cx) / 2
    svg.append(f'<path d="M{px} {py} H{midx} V{cy} H{cx}" stroke="{color}" stroke-width="1.3" fill="none" opacity="0.75"/>')
    svg.append(one_bar(px, py, pdir, color))
    svg.append(crow(cx, cy, cdir, color))

# ── table boxes ───────────────────────────────────────────────────────────
for t in TABLE_POS:
    x, y, w, h = box(t)
    color = TABLE_ZONE_COLOR[t]
    svg.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="9" fill="{CARD}" stroke="{BORDER}" stroke-width="1" filter="url(#sh)"/>')
    svg.append(f'<path d="M{x} {y+9} a9 9 0 0 1 9 -9 h{w-18} a9 9 0 0 1 9 9 v{HEADH-9} h{-w} z" fill="{color}"/>')
    svg.append(f'<text x="{x+12}" y="{y+18}" font-size="12.5" font-weight="700" fill="#FFFFFF">{_esc(t)}</text>')
    pk = set(d[t]["pk"])
    fk = fkcols.get(t, set())
    for i, c in enumerate(d[t]["columns"]):
        cn, ty = c["name"], TYPEMAP.get(c["type"], c["type"])
        ry = y + HEADH + i * ROWH
        if i % 2 == 1:
            svg.append(f'<rect x="{x+1}" y="{ry}" width="{w-2}" height="{ROWH}" fill="{CANVAS}" fill-opacity="0.6"/>')
        cfill, weight = BODY, "400"
        if cn in pk:
            svg.append(f'<text x="{x+8}" y="{ry+11}" font-size="8.5" font-weight="700" fill="{color}">PK</text>')
            cfill, weight = INK, "600"
        elif cn in fk:
            svg.append(f'<text x="{x+8}" y="{ry+11}" font-size="8.5" font-weight="700" fill="{MUTED}">FK</text>')
        svg.append(f'<text x="{x+29}" y="{ry+11}" font-size="10.5" font-weight="{weight}" fill="{cfill}">{_esc(cn)}</text>')
        svg.append(f'<text x="{x+w-8}" y="{ry+11}" font-size="9.5" fill="{MUTED}" text-anchor="end">{_esc(ty)}</text>')

# ── legend ──
lx, ly = 20, H - 60
svg.append(f'<rect x="{lx}" y="{ly}" width="480" height="46" rx="10" fill="{CARD}" stroke="{BORDER}"/>')
svg.append(f'<line x1="{lx+14}" y1="{ly+16}" x2="{lx+56}" y2="{ly+16}" stroke="{NAVY}" stroke-width="1.5"/>')
svg.append(one_bar(lx+16, ly+16, -1, NAVY))
svg.append(crow(lx+56, ly+16, -1, NAVY))
svg.append(f'<text x="{lx+66}" y="{ly+20}" font-size="10.5" fill="{BODY}">one &#8594; many (foreign key)</text>')
svg.append(f'<text x="{lx+14}" y="{ly+38}" font-size="10.5" fill="{BODY}"><tspan font-weight="700" fill="{NAVY}">PK</tspan> primary key &#183; <tspan font-weight="700" fill="{MUTED}">FK</tspan> foreign key &#183; zones mirror DATABASE_DESIGN.md section headers</text>')

svg.append('</svg>')
out_path = "docs/database-schema.svg"
open(out_path, "w", encoding="utf-8").write("\n".join(svg))
print(f"{out_path} written: {W}x{H}, {table_count} tables, {len(FK)} FKs, {len(ZONES)} zones"
      + (f" ({len(_unzoned)} UNZONED: {_unzoned})" if _unzoned else ""))
