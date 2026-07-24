import logging
from typing import Optional

from src.llm.client import call_llm

logger = logging.getLogger(__name__)

# Markers of a response that establishes NO new fact — a denial or
# "no information" answer. Folding these into project memory corrupts it: a
# turn that couldn't answer gets summarized as an established absence ("there
# is no record of X"), which then poisons every later turn (Phase 8, Bug 2).
_DENIAL_MARKERS = (
    "do not contain", "does not contain", "not contain information",
    "no information", "no relevant information", "not enough information",
    "no record of", "there is no mention", "no mention of",
    "couldn't find", "could not find", "unable to find", "cannot find",
    "don't have access", "do not have access", "i don't have information",
    "do not have information", "no such information", "not available in",
    "the provided documents do not", "i couldn't locate", "i could not locate",
)


def _establishes_new_fact(assistant_text: str) -> bool:
    """
    True if the assistant response looks like it stated something worth
    remembering, False if it's a denial/no-information turn. Cheap and
    deterministic — no extra LLM call on the response path.
    """
    t = (assistant_text or "").lower().strip()
    if not t:
        return False
    return not any(marker in t for marker in _DENIAL_MARKERS)


async def update_project_memory(project_id: str, new_messages: list[dict], gateway) -> Optional[str]:
    """
    Updates the rolling summary of a project based on new messages.

    EXPLICIT CONFLICT POLICY:
    If a new fact directly contradicts an existing fact in the summary:
    - Prioritize the newer fact.
    - Append a note indicating it replaced a prior value.
    If they are about different aspects, append the new fact.

    Denial turns are skipped entirely — see `_establishes_new_fact` and
    Phase 8, Bug 2.
    """
    try:
        # 1. Fetch existing memory (works on both REST and direct gateways)
        memory_row = await gateway.get_project_memory(project_id)
        existing_memory = memory_row.get("summary_text", "") if memory_row else ""

        if not new_messages:
            return existing_memory

        # Do not fold a "no information found" / denial turn into memory: it
        # establishes no fact, and summarizing it as an absence corrupts the
        # rolling memory for every later turn (Phase 8, Bug 2).
        assistant_text = " ".join(
            m.get("content", "") for m in new_messages if m.get("role") == "assistant"
        )
        if not _establishes_new_fact(assistant_text):
            logger.info(
                "Skipping project-memory update for %s: response established no new "
                "fact (denial/no-information turn).", project_id
            )
            return existing_memory

        # 2. Extract facts from new messages
        chat_text = "\n".join([f"{msg.get('role', 'user').upper()}: {msg.get('content', '')}" for msg in new_messages])

        prompt = f"""You are an intelligent summarizer maintaining a rolling memory of facts for an ongoing project.

CURRENT MEMORY SUMMARY:
{existing_memory if existing_memory else 'No existing memory.'}

NEW CHAT EXCERPT:
{chat_text}

TASK:
Update the CURRENT MEMORY SUMMARY with any new, relevant factual information established in the NEW CHAT EXCERPT.
Strictly adhere to this conflict policy:
If a new fact directly contradicts an existing fact in the summary, prioritize the newer fact but append a note indicating it replaced a prior value. If they are about different aspects, append the new fact.

Return ONLY the updated comprehensive memory summary text, with no preamble.
"""

        updated_memory = await call_llm(prompt, "Update memory.", temperature=0.1)
        if not updated_memory or not updated_memory.strip():
            return existing_memory

        # 3. Save back to DB
        await gateway.upsert_project_memory(project_id, updated_memory.strip())
        return updated_memory

    except Exception as e:
        logger.error(f"Failed to update project memory: {e}")
        return None
