import os
import uuid
from pathlib import Path
import pandas as pd
from openpyxl.styles import Font, PatternFill

# Absolute path so downloads survive a server started from any working directory
GENERATED_DIR = str(Path(__file__).resolve().parent.parent.parent / "data" / "generated")

# Excel treats a cell as a live formula if it starts with any of these —
# CWE-1236. Table content here originates from LLM/evidence text, not an
# admin author, so it must be neutralized before it ever reaches openpyxl.
_FORMULA_TRIGGER_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _sanitize_cell(value):
    """Force-text escape (leading apostrophe) per OWASP's CWE-1236 guidance."""
    if isinstance(value, str) and value.startswith(_FORMULA_TRIGGER_PREFIXES):
        return "'" + value
    return value


def build_xlsx(payload: dict) -> tuple[str, int]:
    """
    Builds an XLSX file from the JSON payload.
    Returns (filepath, file_size_bytes).
    """
    os.makedirs(GENERATED_DIR, exist_ok=True)
    filename = f"{uuid.uuid4()}.xlsx"
    filepath = os.path.join(GENERATED_DIR, filename)

    # Find the first table section to convert to excel
    table_section = next((s for s in payload.get("sections", []) if s.get("type") == "table"), None)

    if table_section and "headers" in table_section and "rows" in table_section:
        headers = [_sanitize_cell(h) for h in table_section["headers"]]
        rows = [[_sanitize_cell(cell) for cell in row] for row in table_section["rows"]]
        df = pd.DataFrame(rows, columns=headers)
    else:
        # Fallback if no table found, just put title and description
        df = pd.DataFrame([{"Content": _sanitize_cell(payload.get("description", "No table data found."))}])

    with pd.ExcelWriter(filepath, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name="Muhafiz Export")
        
        # Formatting
        workbook = writer.book
        worksheet = writer.sheets["Muhafiz Export"]
        
        # Header formatting
        header_font = Font(bold=True, color="FFFFFF")
        header_fill = PatternFill(start_color="1E3A8A", end_color="1E3A8A", fill_type="solid") # Navy
        
        for cell in worksheet[1]:
            cell.font = header_font
            cell.fill = header_fill
            
        # Adjust column widths
        for col in worksheet.columns:
            max_length = 0
            column = col[0].column_letter
            for cell in col:
                try:
                    if len(str(cell.value)) > max_length:
                        max_length = len(str(cell.value))
                except:
                    pass
            adjusted_width = (max_length + 2)
            worksheet.column_dimensions[column].width = min(adjusted_width, 50)

    file_size = os.path.getsize(filepath)
    return filepath, file_size
