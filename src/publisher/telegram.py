import asyncio
import html as _html
import json
import logging
import math
import re
from pathlib import Path
from urllib.parse import urlparse

import httpx

from src.config import Config

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
MAX_CAPTION_LEN = 1024
MAX_MESSAGE_LEN = 4096
MAX_SELFTEXT_IN_CAPTION = 500
MEDIA_GROUP_MAX = 10


def _api_url(token: str, method: str) -> str:
    return TELEGRAM_API.format(token=token, method=method)


def _md_to_telegram_html(text: str) -> str:
    """Convert Reddit markdown subset to Telegram HTML."""
    pattern = re.compile(
        r'(\*\*(?:.+?)\*\*'
        r'|__(?:.+?)__'
        r'|\*(?:.+?)\*'
        r'|_(?:.+?)_'
        r'|~~(?:.+?)~~'
        r'|`(?:[^`]+)`'
        r'|\[(?:[^\]]+)\]\((?:[^)]+)\)'
        r')',
        re.DOTALL,
    )

    parts = []
    last = 0
    for m in pattern.finditer(text):
        parts.append(_html.escape(text[last:m.start()]))
        token = m.group(0)

        if token.startswith("**") and token.endswith("**"):
            parts.append(f"<b>{_html.escape(token[2:-2])}</b>")
        elif token.startswith("__") and token.endswith("__"):
            parts.append(f"<b>{_html.escape(token[2:-2])}</b>")
        elif token.startswith("*") and token.endswith("*"):
            parts.append(f"<i>{_html.escape(token[1:-1])}</i>")
        elif token.startswith("_") and token.endswith("_"):
            parts.append(f"<i>{_html.escape(token[1:-1])}</i>")
        elif token.startswith("~~") and token.endswith("~~"):
            parts.append(f"<s>{_html.escape(token[2:-2])}</s>")
        elif token.startswith("`") and token.endswith("`"):
            parts.append(f"<code>{_html.escape(token[1:-1])}</code>")
        elif token.startswith("["):
            lm = re.match(r'\[([^\]]+)\]\(([^)]+)\)', token)
            if lm:
                parts.append(f'<a href="{lm.group(2)}">{_html.escape(lm.group(1))}</a>')
        last = m.end()

    parts.append(_html.escape(text[last:]))
    result = "".join(parts)
    # Headings: # text → <b>text</b>
    result = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", result, flags=re.MULTILINE)
    return result


def _build_footer(post: dict, config: Config) -> str:
    reddit_link = f'<a href="{post["url"]}">🔗 r/{post["subreddit"]}</a>'

    if post["post_type"] == "link" and post.get("content_url"):
        domain = urlparse(post["content_url"]).netloc
        if domain.startswith("www."):
            domain = domain[4:]
        external_link = f'<a href="{post["content_url"]}">{domain}</a>'
        reddit_link = f"{reddit_link} : : {external_link}"

    parts = [reddit_link]
    if config.telegram_channel_link:
        parts.append(f'\n<a href="{config.telegram_channel_link}">Лучшее Reddit -></a>')

    return "\n".join(parts)


def _build_caption(post: dict, config: Config) -> str:
    parts = [f'<b>{_html.escape(post["title"])}</b>']

    if post.get("selftext"):
        text = post["selftext"]
        converted = _md_to_telegram_html(text)
        if len(converted) > MAX_SELFTEXT_IN_CAPTION:
            # Truncate raw text, then convert
            text = text[:MAX_SELFTEXT_IN_CAPTION] + "..."
            converted = _md_to_telegram_html(text)
        parts.append(converted)

    parts.append(_build_footer(post, config))
    return "\n\n".join(parts)


def _chunk_text_evenly(body: str, footer: str) -> list[str]:
    """Split body into evenly-sized chunks with footer appended to the last."""
    footer_block = f"\n\n{footer}"
    total = body + footer_block
    if len(total) <= MAX_MESSAGE_LEN:
        return [total]

    # How many chunks do we need?
    n = math.ceil((len(body) + len(footer_block)) / MAX_MESSAGE_LEN)
    chunk_size = math.ceil(len(body) / n)

    chunks = []
    remaining = body
    while remaining:
        if len(remaining) + len(footer_block) <= MAX_MESSAGE_LEN:
            chunks.append(remaining + footer_block)
            remaining = ""
        elif len(remaining) <= chunk_size:
            chunks.append(remaining + footer_block)
            remaining = ""
        else:
            split = remaining.rfind(" ", 0, chunk_size)
            if split == -1:
                split = chunk_size
            chunks.append(remaining[:split])
            remaining = remaining[split:].lstrip()

    return chunks


async def _send_photo(client: httpx.AsyncClient, config: Config, caption: str, photo_path: Path) -> int | None:
    response = await client.post(
        _api_url(config.telegram_bot_token, "sendPhoto"),
        data={
            "chat_id": config.telegram_chat_id,
            "caption": caption[:MAX_CAPTION_LEN],
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        },
        files={"photo": photo_path.read_bytes()},
    )
    if response.status_code == 200:
        return response.json()["result"]["message_id"]
    logger.warning("sendPhoto failed: %s", response.text)
    return None


async def _send_photo_url(client: httpx.AsyncClient, config: Config, caption: str, photo_url: str) -> int | None:
    response = await client.post(
        _api_url(config.telegram_bot_token, "sendPhoto"),
        data={
            "chat_id": config.telegram_chat_id,
            "photo": photo_url,
            "caption": caption[:MAX_CAPTION_LEN],
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        },
    )
    if response.status_code == 200:
        return response.json()["result"]["message_id"]
    logger.warning("sendPhoto (url) failed: %s", response.text)
    return None


async def _send_video(client: httpx.AsyncClient, config: Config, caption: str, video_path: Path) -> int | None:
    response = await client.post(
        _api_url(config.telegram_bot_token, "sendVideo"),
        data={
            "chat_id": config.telegram_chat_id,
            "caption": caption[:MAX_CAPTION_LEN],
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        },
        files={"video": video_path.read_bytes()},
    )
    if response.status_code == 200:
        return response.json()["result"]["message_id"]
    logger.warning("sendVideo failed: %s", response.text)
    return None


async def _send_animation(client: httpx.AsyncClient, config: Config, caption: str, anim_path: Path) -> int | None:
    response = await client.post(
        _api_url(config.telegram_bot_token, "sendAnimation"),
        data={
            "chat_id": config.telegram_chat_id,
            "caption": caption[:MAX_CAPTION_LEN],
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        },
        files={"animation": anim_path.read_bytes()},
    )
    if response.status_code == 200:
        return response.json()["result"]["message_id"]
    logger.warning("sendAnimation failed: %s", response.text)
    return None


async def _send_message(client: httpx.AsyncClient, config: Config, text: str) -> int | None:
    response = await client.post(
        _api_url(config.telegram_bot_token, "sendMessage"),
        data={
            "chat_id": config.telegram_chat_id,
            "text": text[:MAX_MESSAGE_LEN],
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        },
    )
    if response.status_code == 200:
        return response.json()["result"]["message_id"]
    logger.warning("sendMessage failed: %s", response.text)
    return None


async def _send_media_group(
    client: httpx.AsyncClient,
    config: Config,
    paths: list[Path],
    caption: str | None = None,
) -> int | None:
    files = {f"photo{i}": path.read_bytes() for i, path in enumerate(paths)}
    media = []
    for i, key in enumerate(files):
        entry: dict = {"type": "photo", "media": f"attach://{key}"}
        if i == 0 and caption:
            entry["caption"] = caption[:MAX_CAPTION_LEN]
            entry["parse_mode"] = "HTML"
        media.append(entry)

    response = await client.post(
        _api_url(config.telegram_bot_token, "sendMediaGroup"),
        data={"chat_id": config.telegram_chat_id, "media": json.dumps(media)},
        files=files,
    )
    if response.status_code == 200:
        return response.json()["result"][0]["message_id"]
    logger.warning("sendMediaGroup failed: %s", response.text)
    return None


async def _publish_text_messages(
    client: httpx.AsyncClient,
    config: Config,
    post: dict,
) -> int | None:
    title = f'<b>{_html.escape(post["title"])}</b>'
    selftext = _md_to_telegram_html(post.get("selftext") or "")
    footer = _build_footer(post, config)

    body = f"{title}\n\n{selftext}" if selftext else title
    chunks = _chunk_text_evenly(body, footer)

    msg_id = None
    for chunk in chunks:
        msg_id = await _send_message(client, config, chunk)
    return msg_id


async def _publish_gallery(
    client: httpx.AsyncClient,
    config: Config,
    post: dict,
    media_paths: list[Path],
    caption: str,
) -> int | None:
    # If all images fit in one group and caption fits — send together
    if len(media_paths) <= MEDIA_GROUP_MAX and len(caption) <= MAX_CAPTION_LEN:
        return await _send_media_group(client, config, media_paths, caption=caption)

    # Otherwise send all groups without caption, then send text separately
    groups = [media_paths[i:i + MEDIA_GROUP_MAX] for i in range(0, len(media_paths), MEDIA_GROUP_MAX)]
    for group in groups:
        await _send_media_group(client, config, group, caption=None)
    return await _publish_text_messages(client, config, post)


async def _publish_link(
    client: httpx.AsyncClient,
    config: Config,
    post: dict,
    caption: str,
    media_path: Path | None = None,
) -> int | None:
    if media_path:
        return await _send_video(client, config, caption, media_path)

    preview_url = post.get("preview_url")
    if preview_url:
        msg_id = await _send_photo_url(client, config, caption, preview_url)
        if msg_id:
            return msg_id

    return await _send_message(client, config, caption)


async def publish_post(
    config: Config,
    post: dict,
    media_path: Path | None = None,
    media_paths: list[Path] | None = None,
) -> int | None:
    caption = _build_caption(post, config)
    post_type = post["post_type"]

    async with httpx.AsyncClient(timeout=None) as client:
        if post_type == "image" and media_path:
            msg_id = await _send_photo(client, config, caption, media_path)
            if not msg_id:
                # File too large or other error — try URL fallback
                preview_url = post.get("preview_url") or post.get("content_url")
                if preview_url:
                    msg_id = await _send_photo_url(client, config, caption, preview_url)
            if not msg_id:
                msg_id = await _send_message(client, config, caption)
        elif post_type == "video" and media_path:
            msg_id = await _send_video(client, config, caption, media_path)
        elif post_type == "gif" and media_path:
            msg_id = await _send_animation(client, config, caption, media_path)
        elif post_type == "gallery" and media_paths:
            msg_id = await _publish_gallery(client, config, post, media_paths, caption)
        elif post_type == "text":
            msg_id = await _publish_text_messages(client, config, post)
        elif post_type == "link":
            msg_id = await _publish_link(client, config, post, caption, media_path)
        else:
            msg_id = await _send_message(client, config, caption)

    if msg_id:
        logger.info("Published post %s to Telegram (message_id=%d)", post["reddit_id"], msg_id)
    else:
        logger.warning("Failed to publish post %s", post["reddit_id"])

    await asyncio.sleep(config.pause_between_posts)
    return msg_id
