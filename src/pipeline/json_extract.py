import json
import re
from typing import Any


def extract_json(response: str) -> Any:
    """
    Extract JSON (object or array) from an LLM response, tolerating reasoning
    preambles, markdown fences, and trailing prose.

    The old greedy `\\{.*\\}` regex — duplicated across evaluator.py,
    verifier.py, and router.py — grabbed from the FIRST '{' to the LAST '}'
    in the response, so any brace in surrounding prose (or a reasoning-model
    <think> block) produced invalid JSON. This is the single implementation;
    every pipeline call site that parses a JSON LLM response should import it
    from here rather than re-rolling its own extraction.
    """
    if not response:
        raise ValueError("LLM returned empty response")

    # Models with visible reasoning wrap it in <think> tags — drop it.
    response = re.sub(r"<think>.*?</think>", "", response, flags=re.DOTALL).strip()

    # 1. The whole response is JSON
    try:
        return json.loads(response)
    except json.JSONDecodeError:
        pass

    # 2. A fenced ```json block
    fence = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", response, re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1))
        except json.JSONDecodeError:
            pass

    # 3. First balanced top-level object or array (bracket scanning, string-aware)
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = response.find(open_ch)
        while start != -1:
            depth = 0
            in_string = False
            escaped = False
            for i in range(start, len(response)):
                ch = response[i]
                if escaped:
                    escaped = False
                    continue
                if ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = not in_string
                elif not in_string:
                    if ch == open_ch:
                        depth += 1
                    elif ch == close_ch:
                        depth -= 1
                        if depth == 0:
                            candidate = response[start:i + 1]
                            try:
                                return json.loads(candidate)
                            except json.JSONDecodeError:
                                break
            start = response.find(open_ch, start + 1)

    raise ValueError(f"Could not extract valid JSON from LLM response: {response[:200]!r}")
