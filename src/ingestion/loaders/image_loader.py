# ============================================================
# Image Loader — Gemini Vision (new google-genai SDK)
#
# Uses gemini-2.5-flash for vision (handles images, charts, diagrams).
# Migrated from deprecated google-generativeai to google-genai.
#
# Called by:
#   - load_image_with_vision()         → standalone image files
#   - pdf_loader._load_scanned_page_with_vision() → scanned PDF pages
# ============================================================

import base64
import hashlib
import logging
from pathlib import Path

from src.ingestion.document import Document

logger = logging.getLogger(__name__)

_vision_cache: dict[str, str] = {}
MAX_IMAGE_DIMENSION = 2000


def load_image_with_vision(file_path: Path) -> list[Document]:
    """Extract content from an image using Gemini Vision."""
    if not file_path.exists():
        raise FileNotFoundError(f"Image not found: {file_path}")

    image_bytes = file_path.read_bytes()
    ext = file_path.suffix.lower().lstrip(".")
    extracted_text = _describe_image_bytes(image_bytes, ext)

    if not extracted_text:
        logger.warning("Vision extraction returned no text for %s", file_path.name)
        return []

    return [
        Document(
            text=extracted_text,
            metadata={
                "source": file_path.name,
                "source_path": str(file_path),
                "type": "image",
                "image_format": ext,
                "extraction_method": "gemini_vision",
            },
        )
    ]


def _describe_image_bytes(image_bytes: bytes, image_format: str) -> str:
    """
    Send image bytes to Gemini Vision and return the text description.
    Called by both the image loader and the PDF scanned-page fallback.
    """
    # Cache check
    cache_key = hashlib.md5(image_bytes).hexdigest()
    if cache_key in _vision_cache:
        logger.debug("Vision cache hit (md5=%s)", cache_key[:8])
        return _vision_cache[cache_key]

    image_bytes = _resize_if_large(image_bytes, image_format)

    # No locally-served model in this roster has vision capability (Qwen3-14B
    # and Qalb are both text-only). Sending it an image_url doesn't error —
    # it silently hallucinates a plausible-looking non-empty answer, which
    # then gets cached and returned as if it were real OCR (this is what
    # produced garbled/fabricated extraction on genuinely bad-scan pages).
    # Gemini is the only real vision path until a local vision model exists.
    extracted = _call_gemini_vision(image_bytes, image_format)

    if extracted:
        _vision_cache[cache_key] = extracted
    return extracted


def _call_gemini_vision(image_bytes: bytes, image_format: str) -> str:
    """Call Gemini 2.5 Flash with an inline image (new google-genai SDK)."""
    try:
        from google import genai
        from google.genai import types
        from src import config

        client = genai.Client(api_key=config.GEMINI_API_KEY)

        prompt = (
            "Analyze this image thoroughly and extract ALL information:\n"
            "- Text: transcribe completely and accurately.\n"
            "- Charts/graphs: describe the data with specific numbers.\n"
            "- Tables: reproduce in plain text (use | as column separator).\n"
            "- Diagrams/flowcharts: describe structure and relationships.\n"
            "Be comprehensive — this text will be used for semantic search."
        )

        media_type = _get_media_type(image_format)
        b64_data = base64.b64encode(image_bytes).decode("utf-8")

        response = client.models.generate_content(
            model=config.GEMINI_MODEL,
            contents=[
                types.Part.from_bytes(data=image_bytes, mime_type=media_type),
                prompt,
            ],
        )
        text = response.text or ""
        logger.debug("Gemini vision extracted %d chars", len(text))
        return text

    except Exception as exc:
        err_msg = str(exc).lower()
        if "quota" in err_msg or "429" in err_msg or "exhausted" in err_msg:
            # Re-raise rate limits so caller can retry
            raise
        logger.error("Gemini vision failed: %s", exc)
        return _fallback_openai_vision(image_bytes, image_format)


def _fallback_openai_vision(image_bytes: bytes, image_format: str) -> str:
    """Fallback to OpenAI GPT-4o vision if Gemini fails and key exists."""
    try:
        from src import config
        if not config.OPENAI_API_KEY:
            return ""
        from openai import OpenAI

        client = OpenAI(api_key=config.OPENAI_API_KEY)
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        media_type = _get_media_type(image_format)
        response = client.chat.completions.create(
            model=config.OPENAI_MODEL,
            messages=[{"role": "user", "content": [
                {"type": "image_url",
                 "image_url": {"url": f"data:{media_type};base64,{b64}", "detail": "high"}},
                {"type": "text", "text": "Extract all text, data from charts, tables, and diagrams."},
            ]}],
            max_tokens=2000,
        )
        return response.choices[0].message.content or ""
    except Exception as exc:
        logger.error("OpenAI vision fallback failed: %s", exc)
        return ""


def _resize_if_large(image_bytes: bytes, image_format: str) -> bytes:
    try:
        from PIL import Image
        import io

        img = Image.open(io.BytesIO(image_bytes))
        w, h = img.size
        if max(w, h) <= MAX_IMAGE_DIMENSION:
            return image_bytes
        ratio = MAX_IMAGE_DIMENSION / max(w, h)
        img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
        out = io.BytesIO()
        img.save(out, format="JPEG" if image_format in ("jpg", "jpeg") else "PNG")
        logger.debug("Resized image %dx%d → %dx%d", w, h, int(w * ratio), int(h * ratio))
        return out.getvalue()
    except Exception as exc:
        logger.warning("Image resize failed: %s", exc)
        return image_bytes


def _get_media_type(image_format: str) -> str:
    return {"jpg": "image/jpeg", "jpeg": "image/jpeg",
            "png": "image/png", "webp": "image/webp", "gif": "image/gif"
            }.get(image_format.lower(), "image/jpeg")
