"""A DeepEval judge backed by Groq's OpenAI-compatible endpoint.

Why this file exists (Module 109). `gold32_score.py::_judge()` hard-coded
`GeminiModel`, so the only lever a reviewer had was the model *name*. Module 87
chose `gemini-3.1-flash-lite` on quota rather than capability — it measured
stronger Gemini models and could not run them, because the two live Gemini keys
cap the flash tier at 20 requests/DAY against 96 calls per three-pass re-score.
Groq's five keys carry ~1,000 requests/day EACH, so the provider is worth
measuring even if the model turns out not to be worth switching to.

Two things this wrapper does that a bare `LocalModel(base_url=...)` does not:

1. **Key rotation.** It draws its key from `src/llm/key_manager.py`, the same
   rotation every other Groq caller in the project uses, and rotates on a
   429/rate-limit rather than failing the call. The rotation IS the reason to
   look at Groq; reading a single `GROQ_API_KEY` would throw away the whole
   argument for the switch.

2. **Reasoning-trace stripping.** Some Groq models (measured: `qwen/qwen3.6-27b`)
   emit a `<think>…</think>` block INSIDE `message.content`. DeepEval's
   `trim_and_load_json` then fails to parse a score out of it, which surfaces
   downstream as `score is None` — i.e. as an UNSCORED row, indistinguishable
   from a judge outage. Stripped here so the failure mode is visible as what it
   is rather than as missing data.

A note on the Cloudflare trap, because it cost an hour: a plain `urllib`
request to `api.groq.com` returns `403 error code: 1010` — a WAF block on the
user agent, not an auth failure. It looks exactly like five dead keys. The
`openai` SDK (and `httpx`) are fine.
"""
from __future__ import annotations

import os
import re
import sys
from typing import Optional, Tuple, Union

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

GROQ_BASE_URL = os.environ.get(
    "GOLD32_GROQ_BASE_URL", "https://api.groq.com/openai/v1")

# Models on this endpoint interleave a chain-of-thought block into the message
# content. GEval asks for JSON; the block makes the JSON unparseable.
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
# A trace that was never closed (truncated at max_tokens) would otherwise
# survive the pattern above and take the JSON with it.
_OPEN_THINK_RE = re.compile(r"<think>.*\Z", re.DOTALL | re.IGNORECASE)

_RATE_LIMIT_SIGNS = ("rate limit", "rate_limit", "429", "quota",
                     "too many requests", "over capacity", "503")


def strip_reasoning(text: str) -> str:
    """Remove `<think>…</think>` traces from a model reply.

    Deliberately tolerant of an UNCLOSED trace: a reply truncated mid-thought
    has an opening tag and no closing one, and leaving it in place produces the
    same unparseable-JSON failure the closed case does.
    """
    if not text:
        return text
    out = _THINK_RE.sub("", text)
    out = _OPEN_THINK_RE.sub("", out)
    return out.strip()


def _is_rate_limit(exc: BaseException) -> bool:
    err = str(exc).lower()
    return any(s in err for s in _RATE_LIMIT_SIGNS)


def _make_base():
    from deepeval.models import DeepEvalBaseLLM
    return DeepEvalBaseLLM


def build_groq_judge(model: str, temperature: float = 0.0,
                     max_key_rotations: Optional[int] = None):
    """Construct a `GroqJudgeModel`. Imported lazily so that importing
    `gold32_score` costs nothing when the Gemini path is in use."""
    from deepeval.models.llms.utils import trim_and_load_json
    from openai import OpenAI
    from pydantic import BaseModel
    from src.llm.key_manager import key_manager

    DeepEvalBaseLLM = _make_base()

    class GroqJudgeModel(DeepEvalBaseLLM):
        def __init__(self, model_name: str, temperature: float):
            # DeepEvalBaseLLM stores the name on `.model_name` in some
            # versions and only in `load_model()` in others; keep our own.
            self.judge_model_name = model_name
            self.temperature = float(temperature)
            self._keys = list(key_manager.groq_keys)
            if not self._keys:
                raise RuntimeError(
                    "No Groq keys loaded. key_manager reads GROQ_API_KEY_N "
                    "(numbered) and only falls back to the bare GROQ_API_KEY "
                    "when no numbered key exists.")
            # One rotation per key, so a whole-account quota exhaustion fails
            # loudly instead of spinning.
            self._max_rot = (len(self._keys) if max_key_rotations is None
                             else max_key_rotations)
            super().__init__(model_name)

        # DeepEval calls this; we build the client per call so a rotation
        # between calls is actually picked up.
        def load_model(self, async_mode: bool = False):
            key = key_manager.get_current_key("groq") or self._keys[0]
            return OpenAI(api_key=key, base_url=GROQ_BASE_URL)

        def get_model_name(self) -> str:
            return self.judge_model_name

        def _call(self, prompt: str) -> str:
            last = None
            for _ in range(self._max_rot + 1):
                observed = key_manager.get_current_index("groq")
                try:
                    client = self.load_model()
                    resp = client.chat.completions.create(
                        model=self.judge_model_name,
                        messages=[{"role": "user", "content": prompt}],
                        temperature=self.temperature,
                    )
                    return strip_reasoning(resp.choices[0].message.content or "")
                except Exception as e:  # noqa: BLE001
                    last = e
                    if not _is_rate_limit(e):
                        raise
                    # The rotation this whole provider switch exists for.
                    key_manager.rotate_key("groq", observed_index=observed)
            raise last  # every key was rate limited

        def generate(self, prompt: str, schema: "Optional[BaseModel]" = None
                     ) -> Tuple[Union[str, "BaseModel"], float]:
            content = self._call(prompt)
            if schema:
                return schema.model_validate(trim_and_load_json(content)), 0.0
            return content, 0.0

        async def a_generate(self, prompt: str,
                             schema: "Optional[BaseModel]" = None
                             ) -> Tuple[Union[str, "BaseModel"], float]:
            # The scorer runs AnswerRelevancy with async_mode=False and GEval
            # synchronously; this exists so nothing in DeepEval falls over if a
            # metric reaches for the async path.
            return self.generate(prompt, schema)

    return GroqJudgeModel(model, temperature)
