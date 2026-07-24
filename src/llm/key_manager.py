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

    def rotate_key(self, provider: str):
        if provider == "gemini" and self.gemini_keys:
            self.gemini_index = (self.gemini_index + 1) % len(self.gemini_keys)
            logger.warning(f"Rotated Gemini API Key to index {self.gemini_index} (out of {len(self.gemini_keys)} keys).")
        elif provider == "groq" and self.groq_keys:
            self.groq_index = (self.groq_index + 1) % len(self.groq_keys)
            logger.warning(f"Rotated Groq API Key to index {self.groq_index} (out of {len(self.groq_keys)} keys).")

# Singleton instance
key_manager = KeyManager()
