import base64
import json
import logging
from collections.abc import AsyncIterator

import httpx

from src.config import Config

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "Ты помогаешь русскоязычным читателям понять Reddit-посты на английском.\n"
    "\n"
    "Структура ответа (строго в этом порядке, разделы — пустой строкой):\n"
    "\n"
    "1) Простое объяснение\n"
    "30-50 слов общими словами, без сложных терминов и интернет-сленга. "
    "Если для понимания нужен культурный контекст — кратко упомяни. "
    'Без заголовка "Простое объяснение", сразу текст.\n'
    "\n"
    "2) Заголовок\n"
    'Сначала строка "Заголовок:", потом оригинал на английском с новой строки, '
    "потом перевод с новой строки.\n"
    "\n"
    '3) Текст поста (только если в данных есть поле "Текст:")\n'
    'Строка "Текст:". Затем разбей текст на предложения. Для каждого предложения: '
    "оригинал на английском с новой строки, перевод с новой строки, "
    "пустая строка между блоками.\n"
    "\n"
    "4) Текст с картинки (только если на картинке/гифе есть надписи)\n"
    'Если одна картинка: строка "На картинке написано:", дальше оригинал и перевод '
    "(каждая надпись — отдельным блоком).\n"
    'Если несколько картинок (комикс): "На картинке 1 написано:", '
    '"На картинке 2 написано:" и т.д.\n'
    "Если на картинке нет текста — раздел пропустить целиком.\n"
    "\n"
    "Пиши только на русском (кроме оригинальных английских строк). "
    "Без markdown форматирования, без звёздочек, без эмодзи."
)


def _fetch_image(url: str) -> dict | None:
    try:
        resp = httpx.get(url, timeout=10, follow_redirects=True)
        resp.raise_for_status()
        b64 = base64.b64encode(resp.content).decode()
        mime = "image/jpeg"
        url_lower = url.lower()
        if url_lower.endswith(".png"):
            mime = "image/png"
        elif url_lower.endswith(".webp"):
            mime = "image/webp"
        elif url_lower.endswith(".gif"):
            mime = "image/gif"
        return {"inline_data": {"mime_type": mime, "data": b64}}
    except Exception:
        logger.debug("Could not fetch image %s", url)
        return None


def _build_parts(post: dict) -> list[dict]:
    lines = [
        f"Subreddit: r/{post['subreddit']}",
        f"Заголовок: {post['title']}",
        f"Тип поста: {post['post_type']}",
    ]
    if post.get("selftext"):
        lines.append(f"Текст: {post['selftext'][:2000]}")
    lines.append(f"Оценка: {post['score']} upvotes, {post['num_comments']} комментариев")
    text_part = {"text": "\n".join(lines)}

    image_parts: list[dict] = []
    if post.get("post_type") == "gallery" and post.get("media_urls"):
        for url in post["media_urls"][:10]:
            part = _fetch_image(url)
            if part:
                image_parts.append(part)
    else:
        image_url = post.get("preview_url") or (post.get("content_url") if post.get("post_type") == "image" else None)
        if image_url:
            part = _fetch_image(image_url)
            if part:
                image_parts.append(part)

    return [*image_parts, text_part]


def _build_payload(post: dict) -> dict:
    return {
        "system_instruction": {"parts": [{"text": _SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": _build_parts(post)}],
        "generationConfig": {
            "maxOutputTokens": 2048,
            "temperature": 0.4,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }


async def generate_explanation(config: Config, post: dict) -> str:
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-3.1-flash-lite:generateContent?key={config.gemini_api_key}"
    )
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(url, json=_build_payload(post))
        if response.status_code != 200:
            logger.warning("Gemini HTTP %s: %s", response.status_code, response.text)
            response.raise_for_status()

    data = response.json()
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError):
        logger.warning("Unexpected Gemini response: %s", data)
        return "Не удалось сгенерировать объяснение."


async def stream_explanation(config: Config, post: dict) -> AsyncIterator[str]:
    """Stream explanation chunks from Gemini SSE endpoint."""
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-3.1-flash-lite:streamGenerateContent?alt=sse&key={config.gemini_api_key}"
    )
    async with (
        httpx.AsyncClient(timeout=60) as client,
        client.stream("POST", url, json=_build_payload(post)) as response,
    ):
        if response.status_code != 200:
            body = await response.aread()
            logger.warning("Gemini HTTP %s: %s", response.status_code, body.decode())
            response.raise_for_status()

        async for line in response.aiter_lines():
            if not line.startswith("data: "):
                continue
            payload = line[6:]
            try:
                data = json.loads(payload)
                text = data["candidates"][0]["content"]["parts"][0]["text"]
            except (json.JSONDecodeError, KeyError, IndexError):
                continue
            if text:
                yield text
