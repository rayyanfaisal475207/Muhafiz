# ============================================================
# Conversation Memory — Load and Save Session History
#
# WHY KEEP CONVERSATION HISTORY?
# Without memory, every message is treated as the first in a conversation.
# "What about the side effects?" would be meaningless without knowing
# the user just asked about aspirin. Memory makes the chatbot coherent.
#
# STORAGE: PostgreSQL, via PgConversationStore below — see the module
# docstring further down for how the pipeline actually talks to it.
#
# TOKEN BUDGET MANAGEMENT:
# Long conversations can exceed the LLM's context window. We keep
# only the most recent messages that fit within MAX_HISTORY_TOKENS.
# We drop from the OLDEST end, never the newest — the recent context
# is always most relevant.
# ============================================================

import logging
from typing import TypedDict

logger = logging.getLogger(__name__)

# Messages in OpenAI/Anthropic API format
class Message(TypedDict):
    role: str     # "user" or "assistant"
    content: str  # The message text


def format_history_for_prompt(history: list[Message]) -> str:
    """
    Format conversation history as a readable string for insertion into prompts.

    Example output:
        User: Tell me about aspirin.
        Assistant: Aspirin is a nonsteroidal anti-inflammatory drug...
        User: What about the side effects?

    Args:
        history: List of Message dicts.

    Returns:
        Formatted string, or empty string if history is empty.
    """
    if not history:
        return ""

    lines: list[str] = []
    role_labels = {"user": "User", "assistant": "Assistant", "system": "System"}
    for msg in history:
        role = role_labels.get(msg["role"], "Assistant")
        lines.append(f"{role}: {msg['content']}")

    return "\n".join(lines)


def _truncate_to_token_budget(
    history: list[Message],
    max_tokens: int,
) -> list[Message]:
    """
    Remove the oldest messages until the history fits within max_tokens.

    Token estimation: ~4 characters per token (rough but fast approximation).
    We always remove in pairs (user + assistant) to keep the conversation coherent.

    Args:
        history:    Full list of messages.
        max_tokens: Maximum allowed token count.

    Returns:
        Trimmed history list (always ends with the most recent messages).
    """
    def estimate_tokens(msgs: list[Message]) -> int:
        total_chars = sum(len(m["content"]) for m in msgs)
        return total_chars // 4  # ~4 chars per token

    while len(history) > 2 and estimate_tokens(history) > max_tokens:
        # Remove the oldest user+assistant pair (first two elements)
        history = history[2:]
        logger.debug("Truncated oldest message pair from history (budget: %d tokens)", max_tokens)

    return history


# ============================================================
# PostgreSQL Conversation Store — Async Backend
#
# Reads/writes conversation history from the PostgreSQL `sessions` and
# `messages` tables via the DataGateway. All methods are fully async.
#
# The module-level convenience functions at the bottom
# (async_load_history, async_save_history, async_delete_history,
# async_get_sessions) are what the orchestrator actually calls.
# ============================================================

import uuid

from sqlalchemy import select, delete, func as sa_func

from src import config
from src.data_gateway import get_gateway


class PgConversationStore:
    """
    Async conversation store backed by PostgreSQL — operates on the
    ``sessions`` and ``messages`` tables via the DataGateway.
    """

    # ── Load History ──────────────────────────────────────────

    async def load_history(self, session_id: str, user_id: str = None) -> list[Message]:
        gateway = await get_gateway()
        session_obj = await gateway.get_session(session_id)
        if not session_obj:
            return []
        if user_id and session_obj["user_id"] != user_id:
            return []
            
        history = await gateway.get_session_history(session_id)
        messages: list[Message] = [{"role": m["role"], "content": m["content"]} for m in history]

        # Keep only the most recent turns that fit the token budget, dropping
        # from the OLDEST end. Without this, a long conversation grows the
        # prompt without bound (Phase 8, Bug 3 — this truncation existed but
        # was never wired into the load path).
        return _truncate_to_token_budget(messages, config.MAX_HISTORY_TOKENS)

    # ── Save History ──────────────────────────────────────────

    async def save_history(
        self,
        session_id: str,
        user_message: str,
        assistant_response: str,
        user_id: str = None,
        title: str = None,
        project_id: str = None,
    ) -> None:
        gateway = await get_gateway()
        session_obj = await gateway.get_session(session_id)
        if not session_obj:
            await gateway.create_session(session_id, user_id, title or "New Conversation", project_id)
        
        await gateway.save_message(session_id, "user", user_message)
        await gateway.save_message(session_id, "assistant", assistant_response)

    # ── Delete History ────────────────────────────────────────

    async def delete_history(self, session_id: str, user_id: str = None) -> bool:
        gateway = await get_gateway()
        session_obj = await gateway.get_session(session_id)
        if not session_obj:
            return False
        if user_id and session_obj["user_id"] != user_id:
            return False
            
        await gateway.delete_session(session_id)
        return True

    # ── List Sessions ─────────────────────────────────────────

    async def get_sessions(self, user_id: str) -> list[dict]:
        gateway = await get_gateway()
        return await gateway.get_sessions_for_user(user_id)


# ── Module-level convenience wrappers — used by the orchestrator ───────────────

_pg_store = PgConversationStore()

async def async_load_history(session_id: str, user_id: str = None) -> list[Message]:
    """Load history from backend."""
    return await _pg_store.load_history(session_id, user_id)

async def async_save_history(
    session_id: str,
    user_message: str,
    assistant_response: str,
    user_id: str = None,
    project_id: str = None
) -> None:
    """Save history to backend."""
    await _pg_store.save_history(session_id, user_message, assistant_response, user_id, project_id=project_id)

async def async_delete_history(session_id: str, user_id: str = None) -> bool:
    """Delete history from backend."""
    return await _pg_store.delete_history(session_id, user_id)

async def async_get_sessions(user_id: str) -> list[dict]:
    """Get all sessions for a user."""
    return await _pg_store.get_sessions(user_id)
