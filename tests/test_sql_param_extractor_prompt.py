"""
Schema lock for prompts/sql_param_extractor.txt — same discipline as
test_router.py's own few-shot-schema tests: every "Expected Output:" block
in the prompt file must be valid JSON with the documented field set, so a
future edit to the prompt can't silently drift the schema the extractor's
own caller (src/pipeline/sql_extractor.py) expects.

[Legal-code semantic layer] Also locks in the new "legal_code_act" category
few-shot examples specifically — these are the two new examples this
change added.
"""
import json
import re
from pathlib import Path

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "sql_param_extractor.txt"
_PROMPT_TEXT = _PROMPT_PATH.read_text(encoding="utf-8")

_EXPECTED_KEYS = {"category", "subject", "section_ref", "date"}

# Every "Expected Output:\n{...}" block, non-greedy up to the closing brace
# on its own line — matches this prompt file's exact formatting.
_EXAMPLE_RE = re.compile(r"Expected Output:\n(\{.*?\n\})", re.DOTALL)


def _all_examples() -> list[dict]:
    examples = []
    for match in _EXAMPLE_RE.finditer(_PROMPT_TEXT):
        examples.append(json.loads(match.group(1)))
    return examples


def test_every_few_shot_example_is_valid_json_with_expected_keys():
    examples = _all_examples()
    assert len(examples) >= 5  # 3 original + 2 new legal_code_act examples
    for example in examples:
        assert set(example.keys()) == _EXPECTED_KEYS


def test_legal_code_act_examples_use_the_exact_act_name_as_subject():
    examples = [e for e in _all_examples() if e["category"] == "legal_code_act"]
    assert len(examples) == 2
    subjects = {e["subject"] for e in examples}
    assert subjects == {"Arms Ordinance 1965", "PECA 2016"}
    # section_ref is irrelevant at act-level granularity — every act-level
    # example must leave it null, never a specific section.
    assert all(e["section_ref"] is None for e in examples)


def test_penal_code_examples_still_present_and_unaffected():
    """Regression guard: the new category value is additive, the existing
    penal_code few-shots must be untouched."""
    examples = [e for e in _all_examples() if e["category"] == "penal_code"]
    assert len(examples) == 3
