# ============================================================
# Excel and CSV Loader — Tabular Data to Readable Text
#
# WHY CONVERT TABLES TO TEXT?
# Embedding models work on natural language. They don't understand
# column-row relationships. So we convert each row (or batch of rows)
# into a sentence: "Column1: value1 | Column2: value2 | ..."
# This makes table data searchable with natural language queries.
#
# THE HEADER-IN-EVERY-CHUNK RULE:
# Every chunk must include the column headers. Otherwise a chunk that
# reads "45 | Aspirin | 100mg" means nothing without knowing what
# those columns are. With headers: "Age: 45 | Drug: Aspirin | Dose: 100mg"
#
# MERGED CELLS — A KNOWN GOTCHA:
# Excel often has merged cells where the value appears only in the first
# row and the rest are NaN. forward-fill (ffill) propagates the value
# downward, so every row has complete data.
# ============================================================

import logging
from pathlib import Path

from src.ingestion.document import Document

logger = logging.getLogger(__name__)

ROWS_PER_CHUNK = 1  # How many rows to include in each Document chunk


def load_excel(file_path: Path) -> list[Document]:
    """
    Load an Excel file (.xlsx or .xls) into Documents, one chunk per ROWS_PER_CHUNK rows.

    Handles multiple sheets — each sheet is treated as a separate document group.
    Merged cells are forward-filled to avoid NaN gaps.

    Args:
        file_path: Path to the .xlsx or .xls file.

    Returns:
        List of Documents — one per row-batch per sheet.
    """
    try:
        import pandas as pd
    except ImportError:
        raise ImportError(
            "pandas and openpyxl are required for Excel loading. "
            "Install with: pip install pandas openpyxl"
        )

    documents: list[Document] = []

    try:
        # read_excel with sheet_name=None returns a dict: {sheet_name: DataFrame}
        all_sheets: dict = pd.read_excel(str(file_path), sheet_name=None)
        logger.info(
            "Opened Excel %s (%d sheet(s))", file_path.name, len(all_sheets)
        )

        for sheet_name, df in all_sheets.items():
            sheet_docs = _dataframe_to_documents(
                df=df,
                source=file_path.name,
                source_path=str(file_path),
                file_type="excel",
                extra_metadata={"sheet": sheet_name},
            )
            documents.extend(sheet_docs)
            logger.debug(
                "  Sheet '%s': %d rows → %d chunks",
                sheet_name, len(df), len(sheet_docs)
            )

    except Exception as exc:
        logger.error("Failed to load Excel file %s: %s", file_path.name, exc)
        raise

    logger.info("Loaded %d chunks from %s", len(documents), file_path.name)
    return documents


def load_csv(file_path: Path) -> list[Document]:
    """
    Load a CSV file into Documents, one chunk per ROWS_PER_CHUNK rows.

    Args:
        file_path: Path to the .csv file.

    Returns:
        List of Documents — one per row-batch.
    """
    try:
        import pandas as pd
    except ImportError:
        raise ImportError(
            "pandas is required for CSV loading. Install with: pip install pandas"
        )

    try:
        # Try UTF-8 first, fall back to latin-1
        try:
            df = pd.read_csv(str(file_path), encoding="utf-8")
        except UnicodeDecodeError:
            df = pd.read_csv(str(file_path), encoding="latin-1")

        logger.info("Opened CSV %s (%d rows, %d cols)", file_path.name, len(df), len(df.columns))

        documents = _dataframe_to_documents(
            df=df,
            source=file_path.name,
            source_path=str(file_path),
            file_type="csv",
        )
        logger.info("Loaded %d chunks from %s", len(documents), file_path.name)
        return documents

    except Exception as exc:
        logger.error("Failed to load CSV file %s: %s", file_path.name, exc)
        raise


def _dataframe_to_documents(
    df,
    source: str,
    source_path: str,
    file_type: str,
    extra_metadata: dict | None = None,
) -> list[Document]:
    """
    Convert a pandas DataFrame into a list of Documents.

    Each Document covers ROWS_PER_CHUNK rows and includes headers in its text.

    Args:
        df:             The DataFrame to convert.
        source:         Filename string for metadata.
        source_path:    Full path string for metadata.
        file_type:      "excel" or "csv" for metadata.
        extra_metadata: Optional dict merged into metadata (e.g. sheet name).

    Returns:
        List of Document objects.
    """
    import pandas as pd

    if df.empty:
        logger.warning("DataFrame from %s is empty.", source)
        return []

    # Forward-fill to handle merged cells (NaN propagated from empty rows)
    df = df.ffill()

    # Convert all columns to string, handle NaN gracefully
    df = df.astype(str).replace("nan", "")

    # Column headers used in every chunk (so each chunk is self-contained)
    headers = list(df.columns)
    documents: list[Document] = []

    for batch_start in range(0, len(df), ROWS_PER_CHUNK):
        batch_end = min(batch_start + ROWS_PER_CHUNK, len(df))
        batch = df.iloc[batch_start:batch_end]

        # Convert each row to "Column: value | Column: value" format
        row_texts: list[str] = []
        for _, row in batch.iterrows():
            parts = [
                f"{col}: {val}"
                for col, val in zip(headers, row)
                if val.strip()  # skip empty cells
            ]
            row_texts.append(" | ".join(parts))

        chunk_text = "\n".join(row_texts)

        if not chunk_text.strip():
            continue

        metadata = {
            "source": source,
            "source_path": source_path,
            "type": file_type,
            "row_start": batch_start + 1,  # 1-indexed
            "row_end": batch_end,
            "columns": ", ".join(headers),
            **(extra_metadata or {}),
        }

        documents.append(Document(text=chunk_text, metadata=metadata))

    return documents
