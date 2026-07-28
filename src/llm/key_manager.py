import os
import logging
from typing import List

logger = logging.getLogger(__name__)

class KeyManager:
    def __init__(self):
        self.gemini_keys: List[str] = []
        self.groq_keys: List[str] = []
        
        self.gemini_index = 0
        self.groq_index = 0
        
        self._load_keys()

    def _load_keys(self):
        # Load from os.environ since load_dotenv is called in config.py
        for key, value in os.environ.items():
            if key.startswith("GEMINI_API_KEY_") and value:
                self.gemini_keys.append(value)
            elif key.startswith("GROQ_API_KEY_") and value:
                self.groq_keys.append(value)
                
        # Fallback to the main key if no numbered keys exist
        if not self.gemini_keys and os.getenv("GEMINI_API_KEY"):
            self.gemini_keys.append(os.getenv("GEMINI_API_KEY"))
        if not self.groq_keys and os.getenv("GROQ_API_KEY"):
            self.groq_keys.append(os.getenv("GROQ_API_KEY"))
            
        logger.info(f"Loaded {len(self.gemini_keys)} Gemini keys and {len(self.groq_keys)} Groq keys for rotation.")

    def get_current_key(self, provider: str) -> str:
        if provider == "gemini" and self.gemini_keys:
            return self.gemini_keys[self.gemini_index]
        elif provider == "groq" and self.groq_keys:
            return self.groq_keys[self.groq_index]
        return ""

    def get_current_index(self, provider: str) -> int:
        """
        The index a caller should capture BEFORE making a call, so that if
        the call then hits a rate limit, rotate_key() can tell whether it's
        still rotating away from the key that actually failed, or whether a
        concurrent caller already rotated past it.
        """
        if provider == "gemini":
            return self.gemini_index
        elif provider == "groq":
            return self.groq_index
        return 0

    def rotate_key(self, provider: str, observed_index: int = None):
        """
        Compare-and-swap on the index the caller observed before its call
        failed. Without this, concurrent requests that all observed the same
        failing key each increment independently — N concurrent rate-limit
        failures on one key over-rotate N keys ahead instead of the single
        rotation actually needed. observed_index=None skips the check
        (unconditional rotation), kept only so this stays a strict superset
        of the old behaviour for any caller that doesn't pass it.
        """
        if provider == "gemini" and self.gemini_keys:
            if observed_index is not None and observed_index != self.gemini_index:
                logger.info(
                    f"Skipping Gemini key rotation — already rotated past index "
                    f"{observed_index} (now at {self.gemini_index})."
                )
                return
            self.gemini_index = (self.gemini_index + 1) % len(self.gemini_keys)
            logger.warning(f"Rotated Gemini API Key to index {self.gemini_index} (out of {len(self.gemini_keys)} keys).")
        elif provider == "groq" and self.groq_keys:
            if observed_index is not None and observed_index != self.groq_index:
                logger.info(
                    f"Skipping Groq key rotation — already rotated past index "
                    f"{observed_index} (now at {self.groq_index})."
                )
                return
            self.groq_index = (self.groq_index + 1) % len(self.groq_keys)
            logger.warning(f"Rotated Groq API Key to index {self.groq_index} (out of {len(self.groq_keys)} keys).")

# Singleton instance
key_manager = KeyManager()
