import logging
import asyncio
from src.llm.client import call_llm
from src.data_gateway import get_gateway
import json

logger = logging.getLogger(__name__)

async def generate_and_save_title(session_id: str, user_message: str):
    """
    Generates a short semantic title for the chat session and updates the database.
    Returns the generated title.
    """
    system_prompt = "You are a helpful assistant. Generate a short, descriptive title (maximum 5 words) for a chat session starting with the user's message. Do not include quotes or punctuation in the title."
    
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
        fallback = " ".join(user_message.split(' ')[:5]) + "..."
        try:
            gateway = await get_gateway()
            await gateway.update_session_title(session_id, fallback)
        except Exception as save_exc:
            logger.error("Failed to save fallback title: %s", save_exc)
        return fallback
