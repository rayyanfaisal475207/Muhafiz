import os
import uuid
from pathlib import Path
from xml.sax.saxutils import escape
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors

# Absolute path so downloads survive a server started from any working directory
GENERATED_DIR = str(Path(__file__).resolve().parent.parent.parent / "data" / "generated")

def build_pdf(payload: dict) -> tuple[str, int]:
    """
    Builds a PDF file from the JSON payload.
    Returns (filepath, file_size_bytes).
    """
    os.makedirs(GENERATED_DIR, exist_ok=True)
    filename = f"{uuid.uuid4()}.pdf"
    filepath = os.path.join(GENERATED_DIR, filename)

    doc = SimpleDocTemplate(filepath, pagesize=letter)
    elements = []
    
    styles = getSampleStyleSheet()
    title_style = styles['Title']
    h1_style = styles['Heading1']
    h2_style = styles['Heading2']
    h3_style = styles['Heading3']
    normal_style = styles['Normal']
    
    # Paragraph() parses its input as XML markup — raw '&', '<', '>' in report
    # text ("Offense & Penalty", "value < 600,000") crash the build unless escaped.
    # Title
    title = payload.get("title", "Muhafiz Export")
    elements.append(Paragraph(escape(title), title_style))
    elements.append(Spacer(1, 12))

    # Description
    desc = payload.get("description", "")
    if desc:
        elements.append(Paragraph(escape(desc), normal_style))
        elements.append(Spacer(1, 12))

    # Sections
    for sec in payload.get("sections", []):
        stype = sec.get("type")

        if stype == "heading":
            level = sec.get("level", 2)
            style = h1_style if level == 1 else h2_style if level == 2 else h3_style
            elements.append(Paragraph(escape(sec.get("content", "")), style))
            elements.append(Spacer(1, 6))

        elif stype == "paragraph":
            elements.append(Paragraph(escape(sec.get("content", "")), normal_style))
            elements.append(Spacer(1, 6))
            
        elif stype == "table":
            headers = sec.get("headers", [])
            rows = sec.get("rows", [])
            
            if not headers and not rows:
                continue
                
            table_data = []
            if headers:
                table_data.append(headers)
            table_data.extend(rows)
            
            t = Table(table_data)
            t.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1E3A8A')),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
                ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
                ('GRID', (0, 0), (-1, -1), 1, colors.black),
            ]))
            elements.append(t)
            elements.append(Spacer(1, 12))

    doc.build(elements)
    file_size = os.path.getsize(filepath)
    return filepath, file_size
