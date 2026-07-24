# ============================================================
# Shared JSON-response parsing for Qwen3-14B structured-extraction calls.
#
# Every LLM call site in this phase (doc_classifier, ner, domain_entities,
# resolution adjudicator) follows the same contract already established by
# src/pipeline/sql_extractor.py: markdown-fence-stripped JSON, parsed with
# a logged failure rather than a raised one — a malformed response should
# degrade the one extraction it belongs to, not the whole batch.
# ============================================================

import json
import logging
from typing import Optional, Union

logger = logging.getLogger(__name__)


def parse_json_response(response: str, context: str = "") -> Optional[Union[dict, list]]:
    """
    Strip a ```json ... ``` fence if present and parse the result.

    Returns None (logged) on any parse failure rather than raising — callers
    treat a None result as "this extraction produced nothing usable" and
    move on, matching ingestion's per-document resilience requirement.
    """
    if not response:
        return None
    try:
        cleaned = response.strip()
        if cleaned.startswith("```"):
            # Handles both ```json\n...\n``` and bare ```...```.
            cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        return json.loads(cleaned.strip())
    except Exception as exc:
        logger.error("Failed to parse LLM JSON response%s: %s. Raw: %s",
                     f" ({context})" if context else "", exc, response[:500])
        return None
