import base64
import logging

import httpx

from src.config import Config

logger = logging.getLogger(__name__)

_TRANSLATE_PROMPT = (
    "Recreate this image with all English text translated to Russian. "
    "Keep the exact same visual style, font weight, positioning, colors, and layout. "
    "Only change the language of the text."
)


async def generate_translated_image(image_url: str, config: Config) -> bytes | None:
    from src.explainer.gemini import _fetch_image

    image_part = _fetch_image(image_url)
    if not image_part:
        return None

    payload = {
        "contents": [{"role": "user", "parts": [image_part, {"text": _TRANSLATE_PROMPT}]}],
        "generationConfig": {"responseModalities": ["IMAGE"]},
    }
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-3.1-flash-image-preview:generateContent?key={config.gemini_api_key}"
    )
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(url, json=payload)
        if resp.status_code != 200:
            logger.warning("Gemini image gen HTTP %s: %s", resp.status_code, resp.text[:300])
            return None
        for part in resp.json()["candidates"][0]["content"]["parts"]:
            if "inlineData" in part:
                return base64.b64decode(part["inlineData"]["data"])
            if "inline_data" in part:
                return base64.b64decode(part["inline_data"]["data"])
    except Exception as e:
        logger.warning("Gemini image generation failed: %s", e)
    return None
