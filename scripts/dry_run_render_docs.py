"""Dry-run: render the 3 ground-truth documents into their target artifacts.

FIR-2026-ARMS-001            -> clean, text-layer PDF (Naskh font, reportlab, selectable text)
WITNESS-FIR-2026-ARMS-001-01 -> scanned, image-only PDF (Naskh raster + Augraphy degradation)
WITNESS-FIR-2026-ARMS-001-02 -> handwritten-style, image-only PDF (Nastaliq via HarfBuzz raster + Augraphy)

Writes into data/memory/<doc_type_folder>/<doc_id>.pdf (the real ingestible
location per SYNTHETIC_DATASET_PLAN.md §5.3).
"""
import json
from pathlib import Path

import arabic_reshaper
from bidi.algorithm import get_display
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.pagesizes import A4
import fitz  # PyMuPDF
from PIL import Image
import numpy as np
from augraphy import (
    AugraphyPipeline, BadPhotoCopy, InkBleed, NoiseTexturize, BrightnessTexturize,
    Jpeg, Brightness, Gamma, DirtyRollers,
)

import uharfbuzz as hb
import freetype

ROOT = Path(__file__).resolve().parent.parent
GT_DIR = ROOT / "data" / "memory" / "_ground_truth"
FONTS_DIR = ROOT / "data" / "memory" / "_fonts"
SCRATCH_DIR = ROOT / "data" / "memory" / "_dry_run_scratch"
SCRATCH_DIR.mkdir(parents=True, exist_ok=True)

NASKH = str(FONTS_DIR / "NotoNaskhArabic.ttf")
NASTALIQ = str(FONTS_DIR / "NotoNastaliqUrdu.ttf")

DISCLAIMER_EN = "SYNTHETIC / FICTIONAL DOCUMENT — Generated for AI training and demo purposes only. Not an official police record."

pdfmetrics.registerFont(TTFont("NotoNaskhArabic", NASKH))


def rtl(text: str) -> str:
    return get_display(arabic_reshaper.reshape(text))


# ── Clean text-layer PDF (reportlab, Naskh, embedded selectable text) ──────

def render_clean_pdf(doc: dict, out_path: Path):
    W, H = A4
    c = canvas.Canvas(str(out_path), pagesize=A4)
    margin = 40
    y = H - margin

    c.setFont("NotoNaskhArabic", 14)
    c.drawCentredString(W / 2, y, rtl(f"{doc['doc_type']} — {doc.get('police_station','')}"))
    y -= 28
    c.setLineWidth(0.8)
    c.line(margin, y, W - margin, y)
    y -= 24

    # Structured fields box
    box_top = y
    c.setFont("NotoNaskhArabic", 11)
    row_h = 20
    fields = doc["structured_fields"]
    for label, value in fields.items():
        c.drawRightString(W - margin - 6, y - 14, rtl(str(value)))
        y -= row_h
    box_bottom = y
    c.rect(margin, box_bottom, W - 2 * margin, box_top - box_bottom, stroke=1, fill=0)
    y -= 16

    # Narrative box
    narrative_key = "narrative_tehrir" if "narrative_tehrir" in doc else "narrative_statement"
    narrative = doc[narrative_key]
    c.setFont("NotoNaskhArabic", 10)
    max_width = W - 2 * margin - 20
    words = narrative.split(" ")
    lines, cur = [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if pdfmetrics.stringWidth(rtl(trial), "NotoNaskhArabic", 10) > max_width and cur:
            lines.append(cur)
            cur = w
        else:
            cur = trial
    if cur:
        lines.append(cur)

    narrative_top = y
    ny = y - 16
    for line in lines:
        c.drawRightString(W - margin - 10, ny, rtl(line))
        ny -= 15
    narrative_bottom = ny - 4
    c.rect(margin, narrative_bottom, W - 2 * margin, narrative_top - narrative_bottom, stroke=1, fill=0)

    # Footer disclaimer
    c.setFont("Helvetica", 7)
    c.drawCentredString(W / 2, 20, DISCLAIMER_EN)

    c.save()


# ── Raster rendering for noise tiers (image, then embedded into a PDF) ─────

def raster_naskh(doc: dict, canvas_w=1654, canvas_h=2339) -> Image.Image:
    """Naskh-font raster via HarfBuzz shaping (correct + consistent with the clean tier's font)."""
    return _hb_raster(doc, NASKH, canvas_w, canvas_h, font_size=34)


def raster_nastaliq(doc: dict, canvas_w=1654, canvas_h=2339) -> Image.Image:
    return _hb_raster(doc, NASTALIQ, canvas_w, canvas_h, font_size=38)


def _shape_line(text, font_hb, upem):
    buf = hb.Buffer()
    buf.add_str(text)
    buf.guess_segment_properties()
    hb.shape(font_hb, buf, {"kern": True, "liga": True})
    return buf.glyph_infos, buf.glyph_positions


def _draw_shaped_line(img: Image.Image, ft_face, infos, positions, scale, right_x, base_y, fg=(15, 15, 15)):
    total_advance = sum(p.x_advance for p in positions) * scale
    pen_x = right_x - total_advance
    pen_y = base_y
    for info, pos in zip(infos, positions):
        ft_face.load_glyph(info.codepoint, freetype.FT_LOAD_RENDER)
        bmp = ft_face.glyph.bitmap
        left = ft_face.glyph.bitmap_left
        top = ft_face.glyph.bitmap_top
        gx = int(pen_x + pos.x_offset * scale + left)
        gy = int(pen_y - pos.y_offset * scale - top)
        if bmp.width > 0 and bmp.rows > 0:
            glyph_img = Image.frombytes("L", (bmp.width, bmp.rows), bytes(bmp.buffer))
            colored = Image.new("RGB", glyph_img.size, fg)
            img.paste(colored, (gx, gy), glyph_img)
        pen_x += pos.x_advance * scale
        pen_y -= pos.y_advance * scale


def _wrap_by_pixel_width(text, font_hb, upem, scale, max_w):
    words = text.split(" ")
    lines, cur = [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        infos, positions = _shape_line(trial, font_hb, upem)
        width = sum(p.x_advance for p in positions) * scale
        if width > max_w and cur:
            lines.append(cur)
            cur = w
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines


def _hb_raster(doc: dict, font_path: str, canvas_w: int, canvas_h: int, font_size: int) -> Image.Image:
    with open(font_path, "rb") as f:
        fontdata = f.read()
    face_hb = hb.Face(fontdata)
    font_hb = hb.Font(face_hb)
    upem = face_hb.upem or 1000
    font_hb.scale = (upem, upem)

    ft_face = freetype.Face(font_path)
    ft_face.set_pixel_sizes(0, font_size)
    scale = font_size / upem

    img = Image.new("RGB", (canvas_w, canvas_h), (255, 255, 255))
    margin = 90
    right_x = canvas_w - margin
    y = 150
    line_h = int(font_size * 1.8)
    max_w = canvas_w - 2 * margin

    # Title
    title = f"{doc['doc_type']} - {doc.get('police_station', '')}"
    infos, positions = _shape_line(title, font_hb, upem)
    _draw_shaped_line(img, ft_face, infos, positions, scale, right_x, y)
    y += line_h * 2

    # Structured fields
    for label, value in doc["structured_fields"].items():
        line = str(value)
        infos, positions = _shape_line(line, font_hb, upem)
        _draw_shaped_line(img, ft_face, infos, positions, scale, right_x, y)
        y += line_h

    y += line_h // 2

    # Narrative, wrapped
    narrative_key = "narrative_tehrir" if "narrative_tehrir" in doc else "narrative_statement"
    for line in _wrap_by_pixel_width(doc[narrative_key], font_hb, upem, scale, max_w):
        infos, positions = _shape_line(line, font_hb, upem)
        _draw_shaped_line(img, ft_face, infos, positions, scale, right_x, y)
        y += line_h

    return img


def wrap_pdf_from_image(img: Image.Image, out_path: Path):
    """Save a raster image as a single-page image-only PDF (no text layer)."""
    img.convert("RGB").save(str(out_path), "PDF", resolution=200.0)


def degrade(img: Image.Image, seed: int, severity: str = "scanned") -> Image.Image:
    """Curated, restrained degradation — NOT augraphy's default_augraphy_pipeline().

    That default pipeline (tested and rejected during the dry run) is tuned for
    a much messier document genre: ruled notebook backgrounds, highlighter
    marks, and BleedThrough-simulated ghost text from an unrelated random
    source image composited onto the page. That corrupts ground-truth pairing
    outright (the "noise" adds fabricated content, not just legibility loss),
    which is worse than useless for measuring OCR error against a known
    ground truth. This pipeline hand-picks augmenters appropriate for a
    photocopied/scanned or moderately poorly photographed government
    document: paper texture, brightness/contrast/gamma shift, ink bleed,
    compression artifacts. Nothing here draws lines, marks, or unrelated text.
    """
    import random
    random.seed(seed)
    np.random.seed(seed)

    work_img = img
    if severity == "handwritten":
        # Slight rotation, simulating a hand-held photo rather than a flatbed
        # scan — small enough not to clip content given the page margins used.
        work_img = img.rotate(1.6, expand=False, fillcolor=(255, 255, 255))

    arr = np.array(work_img.convert("RGB"))

    if severity == "scanned":
        pipeline = AugraphyPipeline(
            ink_phase=[InkBleed(intensity_range=(0.2, 0.4), p=0.7)],
            paper_phase=[
                NoiseTexturize(sigma_range=(4, 6), turbulence_range=(3, 6), p=0.8),
                BrightnessTexturize(texturize_range=(0.9, 0.99), deviation=0.05, p=0.6),
            ],
            post_phase=[
                BadPhotoCopy(noise_value=(20, 80), noise_sparsity=(0.3, 0.6), noise_concentration=(0.3, 0.6), p=0.9),
                Jpeg(quality_range=(25, 45), p=1.0),
                Brightness(brightness_range=(0.75, 0.95), p=0.7),
                Gamma(gamma_range=(0.8, 1.3), p=0.5),
            ],
        )
    else:  # "handwritten" — harder tier: Nastaliq's script complexity is the
        # primary difficulty driver here, not extreme pixel noise (DirtyRollers'
        # vertical banding was tested and rejected — it overwhelmed legibility
        # even for a human, which stops being a useful stress test). Kept
        # moderately stronger than "scanned" but still human-readable.
        pipeline = AugraphyPipeline(
            ink_phase=[InkBleed(intensity_range=(0.3, 0.45), p=0.8)],
            paper_phase=[
                NoiseTexturize(sigma_range=(3, 5), turbulence_range=(3, 6), p=0.8),
                BrightnessTexturize(texturize_range=(0.88, 0.97), deviation=0.04, p=0.7),
            ],
            post_phase=[
                BadPhotoCopy(noise_value=(15, 90), noise_sparsity=(0.35, 0.6), noise_concentration=(0.35, 0.6), p=1.0),
                Jpeg(quality_range=(18, 32), p=1.0),
                Brightness(brightness_range=(0.68, 0.88), p=0.8),
                Gamma(gamma_range=(0.75, 1.4), p=0.5),
            ],
        )

    out = pipeline.augment(arr)["output"]
    if out.shape != arr.shape:
        out = np.array(Image.fromarray(out).resize((arr.shape[1], arr.shape[0])))
    return Image.fromarray(out)


def main():
    fir = json.loads((GT_DIR / "FIR-2026-ARMS-001.json").read_text(encoding="utf-8"))
    w1 = json.loads((GT_DIR / "WITNESS-FIR-2026-ARMS-001-01.json").read_text(encoding="utf-8"))
    w2 = json.loads((GT_DIR / "WITNESS-FIR-2026-ARMS-001-02.json").read_text(encoding="utf-8"))

    firs_dir = ROOT / "data" / "memory" / "firs"
    ws_dir = ROOT / "data" / "memory" / "witness_statements"
    firs_dir.mkdir(parents=True, exist_ok=True)
    ws_dir.mkdir(parents=True, exist_ok=True)

    # 1. FIR - clean
    clean_path = firs_dir / f"{fir['doc_id']}.pdf"
    render_clean_pdf(fir, clean_path)
    print("wrote", clean_path)

    # 2. Witness 1 - handwritten-style (Nastaliq raster + Augraphy) — was
    # "scanned" (Naskh raster), converted per the scanned->handwritten
    # rendering-style consolidation (only one degraded-render style going
    # forward, not two).
    img1 = raster_nastaliq(w1)
    img1_degraded = degrade(img1, seed=1, severity="handwritten")
    img1.save(SCRATCH_DIR / "w1_pre_degrade.png")
    img1_degraded.save(SCRATCH_DIR / "w1_post_degrade.png")
    handwritten1_path = ws_dir / f"{w1['doc_id']}.pdf"
    wrap_pdf_from_image(img1_degraded, handwritten1_path)
    print("wrote", handwritten1_path)

    # 3. Witness 2 - handwritten-style (Nastaliq raster + Augraphy)
    img2 = raster_nastaliq(w2)
    img2_degraded = degrade(img2, seed=2, severity="handwritten")
    img2.save(SCRATCH_DIR / "w2_pre_degrade.png")
    img2_degraded.save(SCRATCH_DIR / "w2_post_degrade.png")
    handwritten_path = ws_dir / f"{w2['doc_id']}.pdf"
    wrap_pdf_from_image(img2_degraded, handwritten_path)
    print("wrote", handwritten_path)


if __name__ == "__main__":
    main()
