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
MEDIA_GROUP_MAX = 10


def _api_url(token: str, method: str) -> str:
    return TELEGRAM_API.format(token=token, method=method)


def _md_to_telegram_html(text: str) -> str:
    """Convert Reddit markdown subset to Telegram HTML."""
    # Remove Reddit markdown backslash escapes (e.g. \- \( \) \. \# etc.)
    text = re.sub(r"\\([^a-zA-Z0-9\s])", r"\1", text)
    pattern = re.compile(
        r"(\*\*(?:.+?)\*\*"
        r"|__(?:.+?)__"
        r"|\*(?:.+?)\*"
        r"|_(?:.+?)_"
        r"|~~(?:.+?)~~"
        r"|`(?:[^`]+)`"
        r"|\[(?:[^\]]+)\]\((?:[^)]+)\)"
        r")",
        re.DOTALL,
    )

    parts = []
    last = 0
    for m in pattern.finditer(text):
        parts.append(_html.escape(text[last : m.start()]))
        token = m.group(0)

        if (token.startswith("**") and token.endswith("**")) or (token.startswith("__") and token.endswith("__")):
            parts.append(f"<b>{_html.escape(token[2:-2])}</b>")
        elif (token.startswith("*") and token.endswith("*")) or (token.startswith("_") and token.endswith("_")):
            parts.append(f"<i>{_html.escape(token[1:-1])}</i>")
        elif token.startswith("~~") and token.endswith("~~"):
            parts.append(f"<s>{_html.escape(token[2:-2])}</s>")
        elif token.startswith("`") and token.endswith("`"):
            parts.append(f"<code>{_html.escape(token[1:-1])}</code>")
        elif token.startswith("["):
            lm = re.match(r"\[([^\]]+)\]\(([^)]+)\)", token)
            if lm:
                parts.append(f'<a href="{lm.group(2)}">{_html.escape(lm.group(1))}</a>')
        last = m.end()

    parts.append(_html.escape(text[last:]))
    result = "".join(parts)
    # Headings: # text → <b>text</b>
    result = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", result, flags=re.MULTILINE)
    # Collapse redundant blank lines (with optional whitespace) into single blank line
    result = re.sub(r"(\n[ \t]*){2,}", "\n\n", result)
    return result


def _build_footer(post: dict, config: Config) -> str:
    reddit_link = f'<a href="{post["url"]}">🔗 r/{post["subreddit"]}</a>'

    if post["post_type"] == "link" and post.get("content_url"):
        domain = urlparse(post["content_url"]).netloc
        if domain.startswith("www."):
            domain = domain[4:]
        if domain and "reddit.com" not in domain:
            external_link = f'<a href="{post["content_url"]}">{domain}</a>'
            reddit_link = f"{reddit_link} : : {external_link}"

    parts = [reddit_link]
    if config.telegram_channel_link:
        parts.append(f'\n<a href="{config.telegram_channel_link}">Лучшее Reddit -></a>')

    return "\n".join(parts)


def _build_media_texts(post: dict, config: Config) -> tuple[str, list[str]]:
    """Build (caption, overflow_messages) for media posts.

    Caption: title + beginning of selftext (as much as fits MAX_CAPTION_LEN).
    Overflow: remaining selftext split evenly, footer in the last message.
    """
    title_html = f"<b>{_html.escape(post['title'])}</b>"
    footer = _build_footer(post, config)
    selftext = post.get("selftext") or ""
    footer_block = f"\n\n{footer}"

    if not selftext:
        return f"{title_html}{footer_block}", []

    selftext_html = _md_to_telegram_html(selftext)
    title_block = f"{title_html}\n\n"

    # Try to fit everything in one caption
    full = f"{title_block}{selftext_html}{footer_block}"
    if len(full) <= MAX_CAPTION_LEN:
        return full, []

    # Caption gets title + as much selftext as fits
    caption_budget = MAX_CAPTION_LEN - len(title_block)
    if caption_budget <= 0:
        return title_html[:MAX_CAPTION_LEN], _chunk_text_evenly(selftext_html, footer)

    if len(selftext_html) <= caption_budget:
        caption_text = selftext_html
        remaining = ""
    else:
        split = selftext_html.rfind(" ", 0, caption_budget)
        if split == -1:
            split = caption_budget
        caption_text = selftext_html[:split]
        remaining = selftext_html[split:].lstrip()

    caption = f"{title_block}{caption_text}"

    if not remaining:
        return caption, [footer]

    # Split remaining evenly across messages, footer on last
    messages = _chunk_text_evenly(remaining, footer)
    return caption, messages


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
        if len(remaining) + len(footer_block) <= MAX_MESSAGE_LEN or len(remaining) <= chunk_size:
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
        files={"photo": photo_path.read_bytes()},  # noqa: ASYNC240
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
            "supports_streaming": "true",
        },
        files={"video": video_path.read_bytes()},  # noqa: ASYNC240
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
        files={"animation": (anim_path.name, anim_path.read_bytes(), "video/mp4")},  # noqa: ASYNC240
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
            entry["caption"] = caption
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
    title_html = f"<b>{_html.escape(post['title'])}</b>"
    selftext_raw = post.get("selftext") or ""
    footer = _build_footer(post, config)

    # Try single message first
    selftext_html = _md_to_telegram_html(selftext_raw)
    body = f"{title_html}\n\n{selftext_html}" if selftext_html else title_html
    if len(f"{body}\n\n{footer}") <= MAX_MESSAGE_LEN:
        return await _send_message(client, config, f"{body}\n\n{footer}")

    # Too long: split RAW text first, then convert each chunk individually.
    # Splitting already-converted HTML would cut tags in half → invalid HTML.
    raw_chunk_size = 3000
    raw_chunks: list[str] = []
    remaining = selftext_raw
    while remaining:
        if len(remaining) <= raw_chunk_size:
            raw_chunks.append(remaining)
            break
        split = remaining.rfind(" ", 0, raw_chunk_size)
        if split == -1:
            split = raw_chunk_size
        raw_chunks.append(remaining[:split])
        remaining = remaining[split:].lstrip()

    if not raw_chunks:
        raw_chunks = [""]

    msg_id = None
    for i, raw_chunk in enumerate(raw_chunks):
        chunk_html = _md_to_telegram_html(raw_chunk)
        text = f"{title_html}\n\n{chunk_html}" if i == 0 else chunk_html
        if i == len(raw_chunks) - 1:
            text += f"\n\n{footer}"
        msg_id = await _send_message(client, config, text)
    return msg_id


async def _publish_gallery(
    client: httpx.AsyncClient,
    config: Config,
    post: dict,
    media_paths: list[Path],
    caption: str,
) -> int | None:
    groups = [media_paths[i : i + MEDIA_GROUP_MAX] for i in range(0, len(media_paths), MEDIA_GROUP_MAX)]

    for group in groups[:-1]:
        await _send_media_group(client, config, group, caption=None)

    return await _send_media_group(client, config, groups[-1], caption=caption)


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
    caption, overflow = _build_media_texts(post, config)
    post_type = post["post_type"]

    async with httpx.AsyncClient(timeout=None) as client:
        if post_type == "image" and media_path:
            msg_id = await _send_photo(client, config, caption, media_path)
            if not msg_id:
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

        # Send overflow messages for non-text posts
        if msg_id and overflow:
            for text in overflow:
                last_id = await _send_message(client, config, text)
                if last_id:
                    msg_id = last_id

    if msg_id:
        logger.info("Published post %s to Telegram (message_id=%d)", post["reddit_id"], msg_id)
    else:
        logger.warning("Failed to publish post %s", post["reddit_id"])

    return msg_id


async def get_discussion_message_id(config: Config, channel_msg_id: int) -> int | None:
    """Find the auto-forwarded message ID in the discussion group."""
    async with httpx.AsyncClient(timeout=10) as client:
        # Get linked discussion group chat_id
        response = await client.get(
            _api_url(config.telegram_bot_token, "getChat"),
            params={"chat_id": config.telegram_chat_id},
        )
        if response.status_code != 200:
            return None
        linked_chat_id = response.json().get("result", {}).get("linked_chat_id")
        if not linked_chat_id:
            return None

        # Poll getUpdates to find the auto-forwarded message
        response = await client.get(
            _api_url(config.telegram_bot_token, "getUpdates"),
            params={"offset": -20, "limit": 20},
        )
        if response.status_code != 200:
            return None

        for update in response.json().get("result", []):
            msg = update.get("message", {})
            if (
                msg.get("is_automatic_forward")
                and msg.get("chat", {}).get("id") == linked_chat_id
                and msg.get("forward_from_message_id") == channel_msg_id
            ):
                return msg["message_id"]

    return None


_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
_GIF_DOMAINS = {"giphy.com", "tenor.com", "i.giphy.com", "media.tenor.com"}
_MEDIA_URL_RE = re.compile(
    r"!\[[^\]]*\]\((https?://[^)]+)\)"  # ![alt](url)
    r"|(?<!\()(?<!\[)(https?://\S+\.(?:jpg|jpeg|png|webp|gif|mp4)(?:\?\S*)?)",  # bare URL
    re.IGNORECASE,
)
# Reddit inline gif/image: ![gif](giphy|ID) or ![img](emote|ID)
_REDDIT_INLINE_RE = re.compile(r"!\[(?:gif|img)\]\(giphy\|([a-zA-Z0-9_-]+)\)")


def _extract_media_url(body: str) -> tuple[str | None, str, str | None]:
    """Extract first media URL from comment body. Returns (url, clean_body, media_type)."""
    # Check for Reddit inline giphy format: ![gif](giphy|ID)
    m = _REDDIT_INLINE_RE.search(body)
    if m:
        giphy_id = m.group(1)
        media_url = f"https://i.giphy.com/media/{giphy_id}/giphy.gif"
        clean_body = body[: m.start()].rstrip() + body[m.end() :].lstrip()
        return media_url, clean_body.strip(), "gif"

    m = _MEDIA_URL_RE.search(body)
    if not m:
        return None, body, None

    media_url = m.group(1) or m.group(2)
    clean_body = body[: m.start()].rstrip() + body[m.end() :].lstrip()
    clean_body = clean_body.strip()

    parsed = urlparse(media_url)
    path_lower = parsed.path.lower()
    domain = parsed.netloc.lower()

    if any(path_lower.endswith(ext) for ext in _IMAGE_EXTS):
        return media_url, clean_body, "image"
    if path_lower.endswith(".gif") or any(d in domain for d in _GIF_DOMAINS):
        return media_url, clean_body, "gif"
    if path_lower.endswith(".mp4"):
        return media_url, clean_body, "video"

    return media_url, clean_body, "image"  # default to image


def _format_comment(comment: dict, body_override: str | None = None) -> str:
    body = body_override if body_override is not None else comment["body"]
    header = f"\U0001f4ac <b>u/{_html.escape(comment['author'])}</b>"
    body_html = _md_to_telegram_html(body) if body else ""
    if body_html:
        return f"{header}\n\n{body_html}"
    return header


async def publish_comment(
    config: Config,
    comment: dict,
    discussion_chat_id: int,
    reply_to_message_id: int,
) -> int | None:
    """Send one comment as reply in the discussion group."""
    media_url, clean_body, media_type = _extract_media_url(comment["body"])
    caption = _format_comment(comment, body_override=clean_body)

    async with httpx.AsyncClient(timeout=None) as client:
        msg_id = None

        if media_url and media_type == "image":
            response = await client.post(
                _api_url(config.telegram_bot_token, "sendPhoto"),
                data={
                    "chat_id": discussion_chat_id,
                    "photo": media_url,
                    "caption": caption[:MAX_CAPTION_LEN],
                    "parse_mode": "HTML",
                    "reply_to_message_id": reply_to_message_id,
                },
            )
            if response.status_code == 200:
                msg_id = response.json()["result"]["message_id"]

        elif media_url and media_type == "gif":
            response = await client.post(
                _api_url(config.telegram_bot_token, "sendAnimation"),
                data={
                    "chat_id": discussion_chat_id,
                    "animation": media_url,
                    "caption": caption[:MAX_CAPTION_LEN],
                    "parse_mode": "HTML",
                    "reply_to_message_id": reply_to_message_id,
                },
            )
            if response.status_code == 200:
                msg_id = response.json()["result"]["message_id"]

        elif media_url and media_type == "video":
            response = await client.post(
                _api_url(config.telegram_bot_token, "sendVideo"),
                data={
                    "chat_id": discussion_chat_id,
                    "video": media_url,
                    "caption": caption[:MAX_CAPTION_LEN],
                    "parse_mode": "HTML",
                    "reply_to_message_id": reply_to_message_id,
                    "supports_streaming": "true",
                },
            )
            if response.status_code == 200:
                msg_id = response.json()["result"]["message_id"]

        # Fallback to text if no media or media send failed
        if not msg_id:
            text = _format_comment(comment) if media_url else caption
            response = await client.post(
                _api_url(config.telegram_bot_token, "sendMessage"),
                data={
                    "chat_id": discussion_chat_id,
                    "text": text[:MAX_MESSAGE_LEN],
                    "parse_mode": "HTML",
                    "reply_to_message_id": reply_to_message_id,
                    "disable_web_page_preview": "true",
                },
            )
            if response.status_code == 200:
                msg_id = response.json()["result"]["message_id"]

    return msg_id
