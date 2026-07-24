# Generate the TaxIQ ER diagram as an SVG from the live-schema dump.
import json

d = json.load(open("schema.json"))
FK = d["_fks"]

# ── design tokens (product identity) ─────────────────────────────────────────
NAVY="#27477D"; NAVY_D="#16294A"; INK="#1C1B18"; BODY="#4B4842"; MUTED="#86827A"; FAINT="#A8A499"
CANVAS="#F4F2ED"; CARD="#FFFFFF"; BORDER="#D5D0C4"
# group accent colours (desaturated, distinct)
G_ID="#27477D"     # identity — navy
G_CHAT="#3F7D58"   # conversation — green
G_KB="#B0762A"     # knowledge — amber
G_OBS="#7C6BA8"    # observability — violet
GROUPS={"id":G_ID,"chat":G_CHAT,"kb":G_KB,"obs":G_OBS}

TYPEMAP={'varchar':'text','bpchar':'char','int4':'int','int8':'bigint','float8':'float',
 'bool':'bool','timestamp':'timestamp','timestamptz':'timestamptz','jsonb':'jsonb','uuid':'uuid',
 'numeric':'numeric','date':'date','vector':'vector(384)','tsvector':'tsvector','text':'text','_text':'text[]'}

fkcols={}
for f in FK: fkcols.setdefault(f["src"],set()).add(f["src_col"])

# ── table placement (x,y = top-left) and group ───────────────────────────────
ROWH=17; HEADH=30; BOXW=232; PAD=6
LAYOUT={
 # identity
 "users":(70,150,"id"),
 "user_context_profiles":(70,455,"id"),
 "projects":(340,150,"id"),
 "project_memory":(340,455,"id"),
 # conversation
 "sessions":(620,150,"chat"),
 "messages":(895,150,"chat"),
 "session_attachments":(620,440,"chat"),
 "generated_files":(895,455,"chat"),
 # observability (bottom band)
 "error_logs":(345,765,"obs"),
 "pipeline_runs":(640,765,"obs"),
 "pipeline_steps":(915,765,"obs"),
 "mcp_tool_calls":(915,1040,"obs"),
 # knowledge base (right column)
 "documents":(1210,150,"kb"),
 "document_chunks":(1462,150,"kb"),
 "ingestion_jobs":(1210,480,"kb"),
 "tax_rates":(1462,480,"kb"),
}
VIEW=("knowledge_base_documents",(1210,785,"kb"))

# index annotations per column
IDX={("document_chunks","embedding"):"HNSW","document_chunks_fts":"GIN"}

def box_h(t): return HEADH + len(d[t]["columns"])*ROWH + PAD
def box(t):
    x,y,g=LAYOUT[t]; return x,y,BOXW,box_h(t),g
def col_y(t,colname):
    x,y,g=LAYOUT[t]
    for i,c in enumerate(d[t]["columns"]):
        if c["name"]==colname: return y+HEADH+i*ROWH+ROWH/2
    return y+HEADH

svg=[]
W,H=1820,1400
svg.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" font-family="Segoe UI, Arial, sans-serif">')
svg.append(f'<rect width="{W}" height="{H}" fill="{CANVAS}"/>')

# defs: soft shadow
svg.append('<defs><filter id="sh" x="-20%" y="-20%" width="140%" height="140%">'
 '<feDropShadow dx="0" dy="2" stdDeviation="3" flood-color="#1C1B18" flood-opacity="0.10"/></filter></defs>')

# ── title ──
svg.append(f'<text x="46" y="58" font-size="30" font-weight="700" fill="{INK}">TaxIQ — Database Schema</text>')
svg.append(f'<text x="48" y="84" font-size="14" fill="{MUTED}">PostgreSQL · 16 tables + 1 view · pgvector hybrid retrieval · captured from the live database</text>')

# ── group zones (background tints) ──
def zone(x,y,w,h,color,label):
    svg.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="14" fill="{color}" fill-opacity="0.05" stroke="{color}" stroke-opacity="0.28" stroke-width="1.5"/>')
    svg.append(f'<text x="{x+16}" y="{y+26}" font-size="13" font-weight="700" letter-spacing="1.5" fill="{color}">{label}</text>')
zone(40,110,540,560, G_ID, "IDENTITY &amp; ACCESS")
zone(600,110,560,560, G_CHAT, "CHAT &amp; CONVERSATION")
zone(300,720,882,560, G_OBS, "OBSERVABILITY &amp; METRICS")
zone(1190,110,600,1170, G_KB, "KNOWLEDGE BASE")

# ── relationship lines (draw before boxes so boxes sit on top of endpoints) ──
def edge_anchor(t, side):
    x,y,w,h,g=box(t)
    return {"l":(x,y+h/2),"r":(x+w,y+h/2)}[side]

def crow(x,y,dirx):
    # crow's foot (many) opening toward -dirx
    dx=12*(-dirx)
    return f'<path d="M{x} {y} l{dx} {-6} M{x} {y} l{dx} {6} M{x} {y} l{dx} 0" stroke="{{c}}" stroke-width="1.6" fill="none"/>'

def one_bar(x,y,dirx):
    return f'<line x1="{x-4*dirx}" y1="{y-6}" x2="{x-4*dirx}" y2="{y+6}" stroke="{{c}}" stroke-width="1.6"/>'

def rel(parent, pcol, child, ccol, color, dashed=False, one_to_one=False):
    # parent = the "one" side (PK), child = the "many" side (FK)
    px_l=LAYOUT[parent][0]; cx_l=LAYOUT[child][0]
    # choose sides so the line exits toward the other box
    if cx_l >= LAYOUT[parent][0]:
        pside,cside="r","l"; pdir,cdir=1,-1
    else:
        pside,cside="l","r"; pdir,cdir=-1,1
    px,py=edge_anchor(parent,pside); cx,cy=edge_anchor(child,cside)
    # use actual column rows for anchor y
    py=col_y(parent,pcol); cy=col_y(child,ccol)
    px=LAYOUT[parent][0]+(BOXW if pside=="r" else 0)
    cx=LAYOUT[child][0]+(BOXW if cside=="r" else 0)
    midx=(px+cx)/2
    dash='stroke-dasharray="5 4" ' if dashed else ''
    path=f'<path d="M{px} {py} H{midx} V{cy} H{cx}" stroke="{color}" stroke-width="1.6" fill="none" {dash}opacity="0.85"/>'
    svg.append(path)
    # markers
    svg.append(one_bar(px,py,pdir).replace("{c}",color))
    svg.append(crow(cx,cy,cdir).replace("{c}",color))
    if one_to_one:
        svg.append(one_bar(cx,cy,cdir).replace("{c}",color))

# real FKs — colour by parent group
for f in FK:
    p,pc,ch,cc=f["tgt"],f["tgt_col"],f["src"],f["src_col"]
    if p not in LAYOUT or ch not in LAYOUT: continue
    color=GROUPS[LAYOUT[p][2]]
    oto = (ch=="user_context_profiles")  # PK==FK => 1:1
    rel(p,pc,ch,cc,color,dashed=False,one_to_one=oto)

# soft refs (no FK) — dashed, muted
SOFT=[("pipeline_runs","run_id","error_logs","run_id"),
      ("sessions","session_id","session_attachments","session_id"),
      ("documents","doc_id","ingestion_jobs","doc_id")]
for p,pc,ch,cc in SOFT:
    rel(p,pc,ch,cc,MUTED,dashed=True)

# ── table boxes ──
def draw_table(t, is_view=False):
    x,y,w,h,g=box(t) if not is_view else (VIEW[1][0],VIEW[1][1],BOXW,HEADH+len(["filename","chunk_count","doc_type","is_global","ingested_at"])*ROWH+PAD,"kb")
    color=GROUPS[g]
    svg.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="9" fill="{CARD}" stroke="{BORDER}" stroke-width="1" filter="url(#sh)"/>')
    # header
    svg.append(f'<path d="M{x} {y+9} a9 9 0 0 1 9 -9 h{w-18} a9 9 0 0 1 9 9 v{HEADH-9} h{-w} z" fill="{color}"/>')
    icon = "◆ " if is_view else ""
    label = t + ("  (view)" if is_view else "")
    svg.append(f'<text x="{x+12}" y="{y+20}" font-size="13.5" font-weight="700" fill="#FFFFFF">{label}</text>')
    if is_view:
        cols=[("filename","text"),("chunk_count","bigint"),("doc_type","text"),("is_global","bool"),("ingested_at","timestamp")]
        pk=set(); fk=set()
    else:
        cols=[(c["name"], TYPEMAP.get(c["type"],c["type"])) for c in d[t]["columns"]]
        pk=set(d[t]["pk"]); fk=fkcols.get(t,set())
    for i,(cn,ty) in enumerate(cols):
        ry=y+HEADH+i*ROWH
        if i%2==1:
            svg.append(f'<rect x="{x+1}" y="{ry}" width="{w-2}" height="{ROWH}" fill="{CANVAS}" fill-opacity="0.6"/>')
        keymark=""
        cfill=BODY; weight="400"
        if cn in pk:
            keymark=f'<text x="{x+9}" y="{ry+12}" font-size="9" font-weight="700" fill="{color}">PK</text>'
            cfill=INK; weight="600"
        elif cn in fk:
            keymark=f'<text x="{x+9}" y="{ry+12}" font-size="9" font-weight="700" fill="{MUTED}">FK</text>'
        svg.append(keymark)
        svg.append(f'<text x="{x+32}" y="{ry+12}" font-size="11.5" font-weight="{weight}" fill="{cfill}">{cn}</text>')
        svg.append(f'<text x="{x+w-10}" y="{ry+12}" font-size="10.5" fill="{MUTED}" text-anchor="end">{ty}</text>')
        # index badge
        if (t,cn)==("document_chunks","embedding"):
            svg.append(f'<rect x="{x+w-118}" y="{ry+1.5}" width="42" height="14" rx="7" fill="{G_KB}" fill-opacity="0.16"/>'
                       f'<text x="{x+w-97}" y="{ry+12}" font-size="9" font-weight="700" fill="{G_KB}" text-anchor="middle">HNSW</text>')
        if (t,cn)==("document_chunks","fts_vector"):
            svg.append(f'<rect x="{x+w-108}" y="{ry+1.5}" width="30" height="14" rx="7" fill="{G_KB}" fill-opacity="0.16"/>'
                       f'<text x="{x+w-93}" y="{ry+12}" font-size="9" font-weight="700" fill="{G_KB}" text-anchor="middle">GIN</text>')

for t in LAYOUT: draw_table(t)
draw_table(VIEW[0], is_view=True)

# view feeds from document_chunks + documents (dashed derive lines)
vx,vy=VIEW[1][0],VIEW[1][1]
dcx=LAYOUT["document_chunks"][0]+BOXW
svg.append(f'<path d="M{dcx} {col_y("document_chunks","source_file")} H{1772} V{vy+30} H{vx+BOXW} " stroke="{G_KB}" stroke-width="1.4" fill="none" stroke-dasharray="3 4" opacity="0.7"/>')
svg.append(crow(vx+BOXW,vy+30,1).replace("{c}",G_KB))
svg.append(f'<text x="{1560}" y="{vy+14}" font-size="10.5" fill="{G_KB}" opacity="0.95">view aggregates chunks by source_file</text>')

# ── legend ──
lx,ly=455,1300
svg.append(f'<rect x="{lx}" y="{ly}" width="470" height="96" rx="10" fill="{CARD}" stroke="{BORDER}"/>')
svg.append(f'<text x="{lx+16}" y="{ly+24}" font-size="12.5" font-weight="700" fill="{INK}">Legend</text>')
# crow's foot sample
svg.append(f'<line x1="{lx+16}" y1="{ly+46}" x2="{lx+70}" y2="{ly+46}" stroke="{NAVY}" stroke-width="1.6"/>')
svg.append(one_bar(lx+18,ly+46,-1).replace("{c}",NAVY))
svg.append(crow(lx+70,ly+46,-1).replace("{c}",NAVY))
svg.append(f'<text x="{lx+82}" y="{ly+50}" font-size="11" fill="{BODY}">one → many (foreign key)</text>')
svg.append(f'<line x1="{lx+16}" y1="{ly+70}" x2="{lx+70}" y2="{ly+70}" stroke="{MUTED}" stroke-width="1.6" stroke-dasharray="5 4"/>')
svg.append(crow(lx+70,ly+70,-1).replace("{c}",MUTED))
svg.append(f'<text x="{lx+82}" y="{ly+74}" font-size="11" fill="{BODY}">soft reference (no FK constraint)</text>')
# PK/FK + index chips
svg.append(f'<text x="{lx+270}" y="{ly+50}" font-size="11" fill="{BODY}"><tspan font-weight="700" fill="{NAVY}">PK</tspan> primary key · <tspan font-weight="700" fill="{MUTED}">FK</tspan> foreign key</text>')
svg.append(f'<rect x="{lx+270}" y="{ly+62}" width="42" height="14" rx="7" fill="{G_KB}" fill-opacity="0.16"/><text x="{lx+291}" y="{ly+72}" font-size="9" font-weight="700" fill="{G_KB}" text-anchor="middle">HNSW</text>')
svg.append(f'<text x="{lx+320}" y="{ly+73}" font-size="11" fill="{BODY}">vector ANN / <tspan font-weight="700" fill="{G_KB}">GIN</tspan> full-text index</text>')

svg.append('</svg>')
open("erd.svg","w",encoding="utf-8").write("\n".join(svg))
print("erd.svg written:", W,"x",H)
