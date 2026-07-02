import asyncio
import html as _html
import io
import json
import logging
import math
import re
from pathlib import Path
from urllib.parse import urlparse

import httpx
from PIL import Image

from src.config import Config

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
MAX_CAPTION_LEN = 1024
MAX_MESSAGE_LEN = 4096
MEDIA_GROUP_MAX = 10
TELEGRAM_MAX_PIXEL_SUM = 10000


def _fit_photo_for_telegram(photo_bytes: bytes) -> bytes:
    """Scale down image so that width + height <= 10000 (Telegram limit)."""
    img = Image.open(io.BytesIO(photo_bytes))
    w, h = img.size
    if w + h <= TELEGRAM_MAX_PIXEL_SUM:
        return photo_bytes
    scale = TELEGRAM_MAX_PIXEL_SUM / (w + h)
    new_size = (int(w * scale), int(h * scale))
    img = img.resize(new_size, Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _api_url(token: str, method: str) -> str:
    return TELEGRAM_API.format(token=token, method=method)


_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _html_to_plain(text: str) -> str:
    """Strip HTML tags and unescape entities so a message can be re-sent without parse_mode."""
    return _html.unescape(_HTML_TAG_RE.sub("", text))


def _is_parse_error(response: httpx.Response) -> bool:
    return response.status_code == 400 and "can't parse entities" in response.text.lower()


async def _post_message(
    client: httpx.AsyncClient,
    config: Config,
    method: str,
    data: dict,
    *,
    files: dict | None = None,
    text_field: str = "caption",
) -> tuple[int | None, httpx.Response]:
    """POST to Telegram, retrying once as plain text if HTML parsing fails.

    A single malformed tag used to make Telegram reject the whole message; the caller then
    dropped the post permanently. Retrying without parse_mode preserves the post as plain text.
    Returns (message_id | None, last_response) so callers can inspect other failures.
    """
    url = _api_url(config.telegram_bot_token, method)
    response = await client.post(url, data=data, files=files)
    if _is_parse_error(response) and text_field in data:
        logger.info("%s hit an HTML parse error, retrying as plain text", method)
        data = {k: v for k, v in data.items() if k != "parse_mode"}
        data[text_field] = _html_to_plain(data[text_field])
        response = await client.post(url, data=data, files=files)
    # Retry on rate-limiting so a 429 doesn't permanently drop the post (galleries already do this).
    for _ in range(3):
        if response.status_code != 429:
            break
        retry_after = response.json().get("parameters", {}).get("retry_after", 5)
        logger.info("%s rate-limited, sleeping %ds", method, retry_after)
        await asyncio.sleep(retry_after + 1)
        response = await client.post(url, data=data, files=files)
    if response.status_code == 200:
        return response.json()["result"]["message_id"], response
    return None, response


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
    # Strip leading/trailing whitespace so concatenation with title/footer
    # doesn't accumulate extra blank lines.
    return result.strip()


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

    if config.gemini_api_key and config.bot_username:
        ai_url = f"https://t.me/{config.bot_username}?startapp={post['reddit_id']}"
        parts.append(f'<a href="{ai_url}">AI-explanation -></a>')

    if config.telegram_channel_link:
        parts.append(f'<a href="{config.telegram_channel_link}">Лучшее Reddit -></a>')

    return "\n\n".join(parts)


def _take_raw_chunk(raw: str, budget: int) -> tuple[str, str]:
    """Take a leading word-boundary slice of RAW markdown whose converted-HTML length
    fits ``budget``. Returns ``(converted_html, remaining_raw)``.

    Splitting the raw text (rather than the already-converted HTML) keeps every chunk
    independently valid HTML — a markdown token straddling the boundary degrades to plain
    text instead of leaving a dangling ``<a>``/``<b>`` tag that Telegram would reject.
    """
    whole = _md_to_telegram_html(raw)
    if len(whole) <= budget:
        return whole, ""

    # Binary-search the largest word-boundary raw prefix whose conversion fits the budget.
    lo, hi, best = 0, len(raw), 0
    while lo <= hi:
        mid = (lo + hi) // 2
        cut = raw.rfind(" ", 0, mid)
        if cut <= 0:
            cut = mid
        if cut and len(_md_to_telegram_html(raw[:cut])) <= budget:
            best = cut
            lo = mid + 1
        else:
            hi = mid - 1

    if best <= 0:
        # A single unbroken token (e.g. a long URL) exceeds the budget — hard-cut it.
        best = min(budget, len(raw))
    return _md_to_telegram_html(raw[:best]), raw[best:].lstrip()


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

    # Caption budget is smaller (MAX_CAPTION_LEN) than overflow messages (MAX_MESSAGE_LEN).
    # Reserve room for the trailing "..." continuation marker.
    caption_budget = MAX_CAPTION_LEN - len(title_block) - len("...")
    if caption_budget <= 0:
        # Title alone fills the caption — escape+wrap the raw title so <b> stays balanced.
        safe_title = _html.escape(post["title"])[: MAX_CAPTION_LEN - len("<b></b>")]
        return f"<b>{safe_title}</b>", _chunk_text_evenly(selftext, footer)

    caption_text, remaining = _take_raw_chunk(selftext, caption_budget)
    caption = f"{title_block}{caption_text}"

    if not remaining:
        with_footer = f"{caption}{footer_block}"
        if len(with_footer) <= MAX_CAPTION_LEN:
            return with_footer, []
        return caption, [footer]

    # Split remaining evenly across overflow messages, footer on last
    messages = _chunk_text_evenly(remaining, footer)
    return f"{caption}...", messages


def _chunk_text_evenly(body: str, footer: str) -> list[str]:
    """Split RAW body into evenly-sized chunks with footer appended to the last.

    Each chunk is converted to HTML independently so tags are never split across messages.
    """
    footer_block = f"\n\n{footer}"
    marker = "\U0000261d\n..."
    whole_html = _md_to_telegram_html(body)
    if len(f"{marker}{whole_html}{footer_block}") <= MAX_MESSAGE_LEN:
        return [f"{marker}{whole_html}{footer_block}"]

    # Aim for even-sized messages: estimate the chunk count from the converted length,
    # then greedily fill each chunk up to that target by splitting the raw text.
    overhead = len(marker) + len("...")
    n = math.ceil((len(whole_html) + len(footer_block)) / (MAX_MESSAGE_LEN - overhead))
    target = math.ceil(len(whole_html) / max(n, 1))

    chunks: list[str] = []
    remaining = body
    while remaining:
        rem_html = _md_to_telegram_html(remaining)
        if len(f"{marker}{rem_html}{footer_block}") <= MAX_MESSAGE_LEN:
            chunks.append(f"{marker}{rem_html}{footer_block}")
            break
        chunk_html, remaining = _take_raw_chunk(remaining, target)
        chunks.append(f"{marker}{chunk_html}...")

    return chunks


async def _send_photo(
    client: httpx.AsyncClient, config: Config, caption: str, photo_path: Path, *, reply_markup: str | None = None
) -> int | None:
    photo_bytes = photo_path.read_bytes()  # noqa: ASYNC240
    data = {
        "chat_id": config.telegram_chat_id,
        "caption": caption[:MAX_CAPTION_LEN],
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }
    if reply_markup:
        data["reply_markup"] = reply_markup
    msg_id, response = await _post_message(client, config, "sendPhoto", data, files={"photo": photo_bytes})
    if msg_id is not None:
        return msg_id
    if "PHOTO_INVALID_DIMENSIONS" in response.text:
        resized = _fit_photo_for_telegram(photo_bytes)
        msg_id, response = await _post_message(client, config, "sendPhoto", data, files={"photo": resized})
        if msg_id is not None:
            return msg_id
    logger.warning("sendPhoto failed: %s", response.text)
    return None


async def _send_photo_url(
    client: httpx.AsyncClient, config: Config, caption: str, photo_url: str, *, reply_markup: str | None = None
) -> int | None:
    data = {
        "chat_id": config.telegram_chat_id,
        "photo": photo_url,
        "caption": caption[:MAX_CAPTION_LEN],
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }
    if reply_markup:
        data["reply_markup"] = reply_markup
    msg_id, response = await _post_message(client, config, "sendPhoto", data)
    if msg_id is not None:
        return msg_id
    logger.warning("sendPhoto (url) failed: %s", response.text)
    return None


async def _send_video(
    client: httpx.AsyncClient, config: Config, caption: str, video_path: Path, *, reply_markup: str | None = None
) -> int | None:
    data = {
        "chat_id": config.telegram_chat_id,
        "caption": caption[:MAX_CAPTION_LEN],
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
        "supports_streaming": "true",
    }
    if reply_markup:
        data["reply_markup"] = reply_markup
    msg_id, response = await _post_message(
        client,
        config,
        "sendVideo",
        data,
        files={"video": video_path.read_bytes()},  # noqa: ASYNC240
    )
    if msg_id is not None:
        return msg_id
    logger.warning("sendVideo failed: %s", response.text)
    return None


async def _send_animation(
    client: httpx.AsyncClient, config: Config, caption: str, anim_path: Path, *, reply_markup: str | None = None
) -> int | None:
    data = {
        "chat_id": config.telegram_chat_id,
        "caption": caption[:MAX_CAPTION_LEN],
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }
    if reply_markup:
        data["reply_markup"] = reply_markup
    msg_id, response = await _post_message(
        client,
        config,
        "sendAnimation",
        data,
        files={"animation": (anim_path.name, anim_path.read_bytes(), "video/mp4")},  # noqa: ASYNC240
    )
    if msg_id is not None:
        return msg_id
    logger.warning("sendAnimation failed: %s", response.text)
    return None


async def _send_message(
    client: httpx.AsyncClient,
    config: Config,
    text: str,
    *,
    reply_markup: str | None = None,
    reply_to: int | None = None,
) -> int | None:
    data: dict = {
        "chat_id": config.telegram_chat_id,
        "text": text[:MAX_MESSAGE_LEN],
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }
    if reply_markup:
        data["reply_markup"] = reply_markup
    if reply_to:
        data["reply_to_message_id"] = reply_to
    msg_id, response = await _post_message(client, config, "sendMessage", data, text_field="text")
    if msg_id is not None:
        return msg_id
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

    for attempt in range(3):
        response = await client.post(
            _api_url(config.telegram_bot_token, "sendMediaGroup"),
            data={"chat_id": config.telegram_chat_id, "media": json.dumps(media)},
            files=files,
        )
        if response.status_code == 200:
            return response.json()["result"][0]["message_id"]
        if response.status_code == 429:
            retry_after = response.json().get("parameters", {}).get("retry_after", 5)
            logger.info("sendMediaGroup rate-limited, sleeping %ds (attempt %d)", retry_after, attempt + 1)
            await asyncio.sleep(retry_after + 1)
            continue
        if _is_parse_error(response) and caption:
            # parse_mode lives inside the media JSON — strip it and retry the caption as plain text.
            logger.info("sendMediaGroup hit an HTML parse error, retrying as plain text")
            media[0]["caption"] = _html_to_plain(caption)
            media[0].pop("parse_mode", None)
            continue
        break
    logger.warning("sendMediaGroup failed: %s", response.text)
    return None


async def _publish_text_messages(
    client: httpx.AsyncClient,
    config: Config,
    post: dict,
    *,
    reply_markup: str | None = None,
) -> int | None:
    title_html = f"<b>{_html.escape(post['title'])}</b>"
    selftext_raw = post.get("selftext") or ""
    footer = _build_footer(post, config)

    # Try single message first
    selftext_html = _md_to_telegram_html(selftext_raw)
    body = f"{title_html}\n\n{selftext_html}" if selftext_html else title_html
    if len(f"{body}\n\n{footer}") <= MAX_MESSAGE_LEN:
        return await _send_message(client, config, f"{body}\n\n{footer}", reply_markup=reply_markup)

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
    last = len(raw_chunks) - 1
    for i, raw_chunk in enumerate(raw_chunks):
        chunk_html = _md_to_telegram_html(raw_chunk)
        if i == 0:
            text = f"{title_html}\n\n{chunk_html}"
            if i == last:
                text += f"\n\n{footer}"
            else:
                text += "..."
        elif i == last:
            text = f"\U0001f4ac\n...{chunk_html}\n\n{footer}"
        else:
            text = f"\U0001f4ac\n...{chunk_html}..."
        msg_id = await _send_message(client, config, text, reply_markup=reply_markup if i == last else None)
    return msg_id


async def _publish_gallery(
    client: httpx.AsyncClient,
    config: Config,
    post: dict,
    media_paths: list[Path],
    caption: str,
) -> int | None:
    groups = [media_paths[i : i + MEDIA_GROUP_MAX] for i in range(0, len(media_paths), MEDIA_GROUP_MAX)]

    # sendMediaGroup requires 2-10 items. If trailing group has 1 item, steal one from the previous group.
    if len(groups) > 1 and len(groups[-1]) == 1:
        groups[-1].insert(0, groups[-2].pop())

    for group in groups[:-1]:
        await _send_media_group(client, config, group, caption=None)
        await asyncio.sleep(1)

    return await _send_media_group(client, config, groups[-1], caption=caption)


async def _publish_link(
    client: httpx.AsyncClient,
    config: Config,
    post: dict,
    caption: str,
    media_path: Path | None = None,
    *,
    reply_markup: str | None = None,
) -> int | None:
    if media_path:
        return await _send_video(client, config, caption, media_path, reply_markup=reply_markup)

    preview_url = post.get("preview_url")
    if preview_url:
        msg_id = await _send_photo_url(client, config, caption, preview_url, reply_markup=reply_markup)
        if msg_id:
            return msg_id

    return await _send_message(client, config, caption, reply_markup=reply_markup)


async def publish_failed_notice(config: Config, post: dict) -> int | None:
    """Send a link-only notice when a post could not be published normally.

    Ensures a post never vanishes silently: even if media/caption sending fails, its Reddit
    link (and external link, for link posts) reaches Telegram for manual review later.
    """
    lines = ["⚠️ <b>Не удалось опубликовать пост</b>"]
    title = (post.get("title") or "").strip()
    if title:
        lines.append(_html.escape(title))
    lines.append(f'<a href="{post["url"]}">{post["url"]}</a>')
    content_url = post.get("content_url")
    if content_url and content_url != post["url"]:
        lines.append(_html.escape(content_url))
    text = "\n".join(lines)
    async with httpx.AsyncClient(timeout=None) as client:
        return await _send_message(client, config, text)


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
        if msg_id and overflow and post_type != "text":
            for text in overflow:
                last_id = await _send_message(client, config, text)
                if last_id:
                    msg_id = last_id

    if msg_id:
        logger.info("Published post %s to Telegram (message_id=%d)", post["reddit_id"], msg_id)
    else:
        logger.warning("Failed to publish post %s", post["reddit_id"])

    return msg_id


_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
_GIF_DOMAINS = {"giphy.com", "tenor.com", "i.giphy.com", "media.tenor.com"}
_MEDIA_URL_RE = re.compile(
    r"!\[[^\]]*\]\((https?://[^)]+)\)"  # ![alt](url)
    r"|(?<!\()(?<!\[)(https?://\S+\.(?:jpg|jpeg|png|webp|gif|mp4)(?:\?\S*)?)",  # bare URL
    re.IGNORECASE,
)
# Reddit inline gif/image: ![gif](giphy|ID) or ![img](emote|ID)
_REDDIT_INLINE_RE = re.compile(r"!\[(?:gif|img)\]\(giphy\|([a-zA-Z0-9_-]+)(?:\|[^)]*)?\)")


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
    # Comments scraped from old.reddit carry the media URL directly (the body is already
    # clean text); fall back to parsing markdown bodies from other sources.
    if comment.get("media_url"):
        media_url, media_type = comment["media_url"], comment.get("media_type")
        clean_body = comment["body"]
    else:
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
            msg_id, _ = await _post_message(
                client,
                config,
                "sendMessage",
                {
                    "chat_id": discussion_chat_id,
                    "text": text[:MAX_MESSAGE_LEN],
                    "parse_mode": "HTML",
                    "reply_to_message_id": reply_to_message_id,
                    "disable_web_page_preview": "true",
                },
                text_field="text",
            )

    return msg_id
