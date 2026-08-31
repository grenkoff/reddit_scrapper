import base64
import json
import logging
import re
from collections.abc import AsyncIterator
from pathlib import Path

import httpx

from src.config import Config

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT_DEFAULT = (Path(__file__).parent / "system_prompt.txt").read_text(encoding="utf-8").strip()

# Matches "4) Текст с картинки …" up to (but not including) the next numbered
# section "5)" / "6)" / … OR the unnumbered final paragraph that starts with a
# capitalized Russian word (e.g. "Пиши только на русском …").
_SECTION_4_PATTERN = re.compile(
    r"\n\n4\) Текст с картинки.*?(?=\n\n\d\)|\n\n[А-ЯЁ])",
    flags=re.DOTALL,
)


_SECTION_4_FORBID_REPLACEMENT = (
    "\n\n4) Текст с картинки\n"
    "СТРОГИЙ ЗАПРЕТ: НЕ переводи, НЕ цитируй и НЕ упоминай никакой текст, "
    "который виден на картинке/гифке. Картинка с переведённым текстом уже показана "
    "пользователю отдельно. Заканчивай свой ответ сразу после раздела 3 (или раздела 2, "
    "если раздела 3 нет). Любое нарушение этого правила приведёт к дублированию."
)


def _strip_section_4(prompt: str) -> str:
    """Replace section 4 with a forbid instruction when image overlay is shown separately."""
    return _SECTION_4_PATTERN.sub(_SECTION_4_FORBID_REPLACEMENT, prompt, count=1)


async def _get_system_prompt() -> str:
    try:
        from src.db import get_setting

        prompt = await get_setting("system_prompt")
        return prompt if prompt else _SYSTEM_PROMPT_DEFAULT
    except Exception:
        return _SYSTEM_PROMPT_DEFAULT


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


def _build_parts(post: dict, comments: list[dict] | None = None) -> list[dict]:
    lines = [
        f"Subreddit: r/{post['subreddit']}",
        f"Заголовок: {post['title']}",
        f"Тип поста: {post['post_type']}",
    ]
    if post.get("selftext"):
        lines.append(f"Текст: {post['selftext'][:2000]}")
    # Reddit's feeds carry no vote or comment counts, so the prompt states no numbers rather than
    # feeding the model the rank weight that stands in for `score` in the publish queue.
    if comments:
        lines.append("")
        lines.append("Топ комментарии (используй как контекст для понимания, не переводи):")
        for i, c in enumerate(comments, 1):
            lines.append(f"{i}. u/{c['author']}: {c['body'][:500]}")
    text_part = {"text": "\n".join(lines)}

    image_parts: list[dict] = []
    if post.get("post_type") == "gallery" and post.get("media_urls"):
        for url in post["media_urls"][:20]:
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


def _build_payload(post: dict, system_prompt: str, comments: list[dict] | None = None) -> dict:
    return {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": _build_parts(post, comments)}],
        "generationConfig": {
            "maxOutputTokens": 2048,
            "temperature": 0.4,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }


async def _safe_fetch_comments(config: Config, post: dict) -> list[dict]:
    from src.scraper.reddit import fetch_top_comments

    try:
        return await fetch_top_comments(config, post, limit=5)
    except Exception:
        logger.debug("Could not fetch comments for explanation context")
        return []


async def generate_explanation(config: Config, post: dict) -> str:
    comments = await _safe_fetch_comments(config, post)
    system_prompt = await _get_system_prompt()
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-3.1-flash-lite:generateContent?key={config.gemini_api_key}"
    )
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(url, json=_build_payload(post, system_prompt, comments))
        if response.status_code != 200:
            logger.warning("Gemini HTTP %s: %s", response.status_code, response.text)
            response.raise_for_status()

    data = response.json()
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError):
        logger.warning("Unexpected Gemini response: %s", data)
        return "Не удалось сгенерировать объяснение."


async def stream_explanation(config: Config, post: dict, skip_image_text: bool = False) -> AsyncIterator[str]:
    """Stream explanation chunks from Gemini SSE endpoint."""
    comments = await _safe_fetch_comments(config, post)
    system_prompt = await _get_system_prompt()
    if skip_image_text:
        system_prompt = _strip_section_4(system_prompt)
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-3.1-flash-lite:streamGenerateContent?alt=sse&key={config.gemini_api_key}"
    )
    async with (
        httpx.AsyncClient(timeout=60) as client,
        client.stream("POST", url, json=_build_payload(post, system_prompt, comments)) as response,
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
