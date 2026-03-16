import asyncio
import logging
from pathlib import Path

import httpx

from src.config import Config

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
MAX_CAPTION_LEN = 1024
MAX_SELFTEXT_LEN = 500


def _build_caption(post: dict) -> str:
    parts = [f"📌 {post['title']}"]

    if post.get("selftext"):
        text = post["selftext"]
        if len(text) > MAX_SELFTEXT_LEN:
            text = text[:MAX_SELFTEXT_LEN] + "..."
        parts.append(text)

    parts.append(f"👍 {post['score']} | 💬 {post['num_comments']} | r/{post['subreddit']}")
    parts.append(f"🔗 {post['url']}")

    caption = "\n\n".join(parts)
    return caption[:MAX_CAPTION_LEN]


def _api_url(token: str, method: str) -> str:
    return TELEGRAM_API.format(token=token, method=method)


async def _send_photo(client: httpx.AsyncClient, config: Config, caption: str, photo_path: Path) -> int | None:
    data = photo_path.read_bytes()  # noqa: ASYNC240
    response = await client.post(
        _api_url(config.telegram_bot_token, "sendPhoto"),
        data={"chat_id": config.telegram_chat_id, "caption": caption},
        files={"photo": data},
    )
    if response.status_code == 200:
        return response.json()["result"]["message_id"]
    logger.warning("sendPhoto failed: %s", response.text)
    return None


async def _send_video(client: httpx.AsyncClient, config: Config, caption: str, video_path: Path) -> int | None:
    data = video_path.read_bytes()  # noqa: ASYNC240
    response = await client.post(
        _api_url(config.telegram_bot_token, "sendVideo"),
        data={"chat_id": config.telegram_chat_id, "caption": caption},
        files={"video": data},
    )
    if response.status_code == 200:
        return response.json()["result"]["message_id"]
    logger.warning("sendVideo failed: %s", response.text)
    return None


async def _send_media_group(client: httpx.AsyncClient, config: Config, caption: str, paths: list[Path]) -> int | None:
    import json

    files = {f"photo{i}": path.read_bytes() for i, path in enumerate(paths[:10])}
    media = []
    for i, key in enumerate(files):
        entry: dict = {"type": "photo", "media": f"attach://{key}"}
        if i == 0:
            entry["caption"] = caption
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


async def _send_message(client: httpx.AsyncClient, config: Config, text: str) -> int | None:
    response = await client.post(
        _api_url(config.telegram_bot_token, "sendMessage"),
        data={"chat_id": config.telegram_chat_id, "text": text, "disable_web_page_preview": False},
    )
    if response.status_code == 200:
        return response.json()["result"]["message_id"]
    logger.warning("sendMessage failed: %s", response.text)
    return None


async def publish_post(
    config: Config,
    post: dict,
    media_path: Path | None = None,
    media_paths: list[Path] | None = None,
) -> int | None:
    caption = _build_caption(post)
    post_type = post["post_type"]

    async with httpx.AsyncClient(timeout=60) as client:
        if post_type == "image" and media_path:
            msg_id = await _send_photo(client, config, caption, media_path)
        elif post_type == "video" and media_path:
            msg_id = await _send_video(client, config, caption, media_path)
        elif post_type == "gallery" and media_paths:
            msg_id = await _send_media_group(client, config, caption, media_paths)
        else:
            msg_id = await _send_message(client, config, caption)

    if msg_id:
        logger.info("Published post %s to Telegram (message_id=%d)", post["reddit_id"], msg_id)
    else:
        logger.warning("Failed to publish post %s", post["reddit_id"])

    await asyncio.sleep(config.pause_between_posts)
    return msg_id
