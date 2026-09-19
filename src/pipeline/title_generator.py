import logging
import asyncio
from src.llm.client import call_llm
from src.data_gateway import get_gateway
import json

logger = logging.getLogger(__name__)

async def generate_and_save_title(session_id: str, user_message: str, assistant_answer: str | None = None):
    """
    Generates a short semantic title for the chat session and updates the database.
    Returns the generated title.
    """
    # Titling from the question ALONE reproduced the question as the title
    # ("What is this case about?"), which is what the provisional title in
    # main.py already does — so the generated title added nothing. Naming a
    # conversation after what it turned out to be ABOUT needs the answer
    # too, which is why the answer is passed in when the caller has it.
    system_prompt = (
        "You name chat conversations. Given the user's first question and the "
        "assistant's answer, reply with a short noun-phrase title of at most 5 "
        "words describing what the conversation is about. Name the subject "
        "matter, never restate the question and never phrase the title as a "
        "question. Reply with the title alone: no quotes, no punctuation, no "
        "preamble."
    )

    # Keep the raw question for the except-branch fallback below — the
    # prompt text `user_message` is about to become is not a usable title.
    original_question = user_message

    if assistant_answer:
        # Trimmed: the title only needs the gist, and the local model's
        # thinking trace competes for the same token budget (see below).
        user_message = "\n\n".join([
            f"First question:\n{user_message.strip()}",
            f"Assistant's answer:\n{assistant_answer.strip()[:1500]}",
        ])
    
    try:
        # Qwen3-14B's thinking trace consumes max_tokens before its actual
        # answer, and this server doesn't honor enable_thinking=False — a
        # tight budget here silently produced an empty/truncated title on
        # every call (this call site was missed in the Phase 0 max_tokens
        # fix applied to every other prompted role). 800 matches the ceiling
        # used everywhere else.
        title = await call_llm(
            system_prompt=system_prompt,
            user_message=user_message,
            temperature=0.7,
            # 800 wasn't enough on live re-measurement — Qwen3-14B's
            # thinking trace can exhaust it before the answer. Raised to
            # 2000 for the LOCAL budget; cloud_max_tokens pinned at the
            # old 800 so the cloud fallback is unaffected.
            max_tokens=2000,
            cloud_max_tokens=800,
            role="reasoning",
        )
        title = title.strip().strip('"').strip("'")
        
        # Update the database
        gateway = await get_gateway()
        await gateway.update_session_title(session_id, title)
                
        return title
    except Exception as e:
        logger.error("Failed to generate title: %s", e)
        fallback = " ".join(original_question.split(' ')[:5]) + "..."
        try:
            gateway = await get_gateway()
            await gateway.update_session_title(session_id, fallback)
        except Exception as save_exc:
            logger.error("Failed to save fallback title: %s", save_exc)
        return fallback
