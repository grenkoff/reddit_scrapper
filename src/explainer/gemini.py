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


_TRANSLATE_PROMPT = (
    "Ты переводишь комментарии с Reddit на русский язык для Telegram-канала.\n"
    "Тебе дают заголовок поста как контекст и список комментариев.\n"
    "Переведи каждый комментарий, сохраняя тон: шутка должна остаться шуткой, "
    "сарказм — сарказмом, грубость — грубостью, не смягчай.\n"
    "Отсылки к играм, фильмам и мемам не переводи буквально — передавай смысл; "
    "имена, никнеймы и названия оставляй как есть.\n"
    "Ответь ТОЛЬКО JSON-массивом строк той же длины и в том же порядке, без пояснений."
)

# Google takes a model offline for hours at a time: on 2026-09-24 gemini-3.1-flash-lite answered
# every request with 503 "This model is currently experiencing high demand" from 07:32 onwards, and
# both the explanations and the comment translations went with it. So each call walks a chain and
# settles on the first model that answers, ending with an older one that tends to stay available.
# Every fallback is one that answered 200 to this module's own payloads on 2026-09-24. The "lite"
# aliases are deliberately absent: gemini-flash-lite-latest and gemini-3.5-flash-lite reject the
# request outright (400), so falling back to them would only turn an outage into a hard error.
_MODEL_CHAIN = ("gemini-3.1-flash-lite", "gemini-3.5-flash", "gemini-2.5-flash")
_RETRIABLE_STATUSES = (429, 500, 502, 503, 504)


def _model_url(model: str, endpoint: str, api_key: str, *, sse: bool = False) -> str:
    query = f"?alt=sse&key={api_key}" if sse else f"?key={api_key}"
    return f"https://generativelanguage.googleapis.com/v1beta/models/{model}:{endpoint}{query}"


async def _post_to_first_available(client: httpx.AsyncClient, api_key: str, payload: dict) -> httpx.Response | None:
    """POST to each model in turn, returning the first non-overloaded answer."""
    response = None
    for model in _MODEL_CHAIN:
        response = await client.post(_model_url(model, "generateContent", api_key), json=payload)
        if response.status_code == 200:
            if model != _MODEL_CHAIN[0]:
                logger.info("Gemini fell back to %s", model)
            return response
        if response.status_code not in _RETRIABLE_STATUSES:
            break
        logger.info("Gemini %s on %s, trying the next model", response.status_code, model)
    if response is not None:
        logger.warning("Gemini HTTP %s: %s", response.status_code, response.text[:200])
    return None


async def translate_comments(config: Config, post: dict, comments: list[dict]) -> list[str | None]:
    """Translate comment bodies into Russian, one request for the whole batch.

    Returns a list aligned with ``comments``; an entry is None when there is nothing to translate
    or the model's answer could not be used, and the caller then publishes the original alone.
    """
    if not config.gemini_api_key:
        return [None] * len(comments)
    indexed = [(i, c["body"]) for i, c in enumerate(comments) if c.get("body")]
    if not indexed:
        return [None] * len(comments)

    payload = {
        "system_instruction": {"parts": [{"text": _TRANSLATE_PROMPT}]},
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": json.dumps(
                            {"post_title": post.get("title", ""), "comments": [b for _, b in indexed]},
                            ensure_ascii=False,
                        )
                    }
                ],
            }
        ],
        "generationConfig": {
            "maxOutputTokens": 2048,
            "temperature": 0.3,
            "responseMimeType": "application/json",
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }

    translations: list[str | None] = [None] * len(comments)
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await _post_to_first_available(client, config.gemini_api_key, payload)
        if response is None:
            return translations
        text = response.json()["candidates"][0]["content"]["parts"][0]["text"]
        parsed = json.loads(text)
        if not isinstance(parsed, list) or len(parsed) != len(indexed):
            # A mismatched list cannot be aligned with the comments, and guessing would caption a
            # comment with someone else's translation.
            logger.warning("Gemini returned %s translations for %d comments", type(parsed).__name__, len(indexed))
            return translations
    except Exception:
        logger.warning("Comment translation failed for %s", post.get("reddit_id"), exc_info=True)
        return translations

    for (position, _), translated in zip(indexed, parsed, strict=True):
        if isinstance(translated, str) and translated.strip():
            translations[position] = translated.strip()
    return translations


async def generate_explanation(config: Config, post: dict) -> str:
    comments = await _safe_fetch_comments(config, post)
    system_prompt = await _get_system_prompt()
    async with httpx.AsyncClient(timeout=30) as client:
        response = await _post_to_first_available(
            client, config.gemini_api_key, _build_payload(post, system_prompt, comments)
        )
    if response is None:
        return "Не удалось сгенерировать объяснение."

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
    payload = _build_payload(post, system_prompt, comments)
    async with httpx.AsyncClient(timeout=60) as client:
        for position, model in enumerate(_MODEL_CHAIN):
            url = _model_url(model, "streamGenerateContent", config.gemini_api_key, sse=True)
            async with client.stream("POST", url, json=payload) as response:
                if response.status_code != 200:
                    body = await response.aread()
                    if response.status_code in _RETRIABLE_STATUSES and position + 1 < len(_MODEL_CHAIN):
                        logger.info("Gemini %s on %s, trying the next model", response.status_code, model)
                        continue
                    logger.warning("Gemini HTTP %s: %s", response.status_code, body.decode()[:200])
                    response.raise_for_status()
                if position:
                    logger.info("Gemini fell back to %s", model)

                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    chunk = line[6:]
                    try:
                        data = json.loads(chunk)
                        text = data["candidates"][0]["content"]["parts"][0]["text"]
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue
                    if text:
                        yield text
            return
