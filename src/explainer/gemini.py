import base64
import logging

import httpx

from src.config import Config

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
Ты объясняешь Reddit-посты русскоязычным читателям.
Дай краткое объяснение сути поста: 2-4 предложения, простым языком.
Если для понимания нужен культурный или интернет-контекст — кратко упомяни его.
Пиши только на русском, без заголовков и списков.\
"""


def _build_parts(post: dict) -> list[dict]:
    lines = [
        f"Subreddit: r/{post['subreddit']}",
        f"Заголовок: {post['title']}",
        f"Тип поста: {post['post_type']}",
    ]
    if post.get("selftext"):
        lines.append(f"Текст: {post['selftext'][:1000]}")
    lines.append(f"Оценка: {post['score']} upvotes, {post['num_comments']} комментариев")
    text_part = {"text": "\n".join(lines)}

    image_url = post.get("preview_url") or (post.get("content_url") if post.get("post_type") == "image" else None)
    if image_url:
        try:
            resp = httpx.get(image_url, timeout=10, follow_redirects=True)
            resp.raise_for_status()
            b64 = base64.b64encode(resp.content).decode()
            mime = "image/jpeg"
            if image_url.lower().endswith(".png"):
                mime = "image/png"
            elif image_url.lower().endswith(".webp"):
                mime = "image/webp"
            return [{"inline_data": {"mime_type": mime, "data": b64}}, text_part]
        except Exception:
            logger.debug("Could not fetch image for Gemini, falling back to text only")

    return [text_part]


async def generate_explanation(config: Config, post: dict) -> str:
    payload = {
        "system_instruction": {"parts": [{"text": _SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": _build_parts(post)}],
        "generationConfig": {
            "maxOutputTokens": 1024,
            "temperature": 0.4,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.5-flash:generateContent?key={config.gemini_api_key}"
    )
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(url, json=payload)
        if response.status_code != 200:
            logger.warning("Gemini HTTP %s: %s", response.status_code, response.text)
            response.raise_for_status()

    data = response.json()
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError):
        logger.warning("Unexpected Gemini response: %s", data)
        return "Не удалось сгенерировать объяснение."
