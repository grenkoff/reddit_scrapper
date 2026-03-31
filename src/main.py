import asyncio
import contextlib
import logging
import random
import signal
import time
from datetime import UTC, datetime

import httpx

from src.config import load_config
from src.db import (
    get_unpublished_posts,
    init_db,
    insert_post,
    is_post_exists,
    log_scrape,
    mark_as_published,
)
from src.publisher.telegram import get_discussion_message_id, publish_comment, publish_post
from src.scraper.media import (
    cleanup,
    compress_video,
    download_gif,
    download_image,
    download_video,
    download_video_direct,
)
from src.scraper.reddit import fetch_top_comments, fetch_top_posts

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

_VIDEO_DOMAINS = {"youtube.com", "youtu.be", "vimeo.com", "twitter.com", "x.com", "tiktok.com", "streamable.com"}


def _is_video_url(url: str) -> bool:
    from urllib.parse import urlparse

    netloc = urlparse(url).netloc.lower()
    return any(d in netloc for d in _VIDEO_DOMAINS)


async def scrape_new_posts(config) -> None:
    """Fetch Reddit and store new posts in DB."""
    started_at = datetime.now(UTC)
    posts_found = posts_new = 0
    error = None
    try:
        posts = await fetch_top_posts(config)
        posts_found = len(posts)
        for post in posts:
            if config.skip_nsfw and post["is_nsfw"]:
                continue
            if await is_post_exists(post["reddit_id"]):
                continue
            await insert_post(post)
            posts_new += 1
        logger.info("Scrape done: found=%d new=%d", posts_found, posts_new)
    except Exception as e:
        error = str(e)
        logger.error("Scrape failed: %s", e, exc_info=True)
    finally:
        await log_scrape(
            started_at=started_at,
            finished_at=datetime.now(UTC),
            posts_found=posts_found,
            posts_new=posts_new,
            posts_published=0,
            error=error,
        )


async def _publish_comments_delayed(config, post: dict, msg_id: int) -> None:
    """Fetch top comments and publish them in discussion group over 10 minutes."""
    try:
        # Wait for Telegram to auto-forward the post to discussion group, retry if not found
        discussion_msg_id = None
        for attempt in range(4):
            await asyncio.sleep(3 * (attempt + 1))
            discussion_msg_id = await get_discussion_message_id(config, msg_id)
            if discussion_msg_id:
                break
        if not discussion_msg_id:
            logger.warning("Could not find discussion message for post %s", post["reddit_id"])
            return

        # Get linked discussion group chat_id
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                f"https://api.telegram.org/bot{config.telegram_bot_token}/getChat",
                params={"chat_id": config.telegram_chat_id},
            )
            discussion_chat_id = response.json().get("result", {}).get("linked_chat_id")
            if not discussion_chat_id:
                return

        comments = await fetch_top_comments(config, post)
        if not comments:
            return
        count = min(random.randint(1, 5), len(comments))
        comments = comments[:count]

        delays = sorted(random.uniform(0, 600) for _ in range(count))
        elapsed = 0.0
        for delay, comment in zip(delays, comments, strict=True):
            await asyncio.sleep(delay - elapsed)
            elapsed = delay
            await publish_comment(config, comment, discussion_chat_id, discussion_msg_id)
        logger.info("Published %d comments for post %s", count, post["reddit_id"])
    except Exception:
        logger.warning("Failed to publish comments for %s", post["reddit_id"], exc_info=True)


async def publish_one(config) -> bool:
    """Pick the next unpublished post and publish it. Returns True if published."""
    posts = await get_unpublished_posts(limit=1)
    if not posts:
        return False

    post = posts[0]
    media_path = None
    media_paths = None

    if post["post_type"] == "image" and post.get("content_url"):
        media_path = await download_image(post["content_url"])
    elif post["post_type"] == "gif" and post.get("content_url"):
        media_path = await download_gif(post["content_url"])
    elif post["post_type"] == "video" and post.get("video_url"):
        media_path = await download_video_direct(post["video_url"], hls_url=post.get("hls_url"))
        if media_path:
            media_path = await asyncio.get_event_loop().run_in_executor(None, compress_video, media_path)
    elif post["post_type"] == "gallery" and post.get("media_urls"):
        paths = [await download_image(url) for url in post["media_urls"]]
        media_paths = [p for p in paths if p is not None] or None
    elif post["post_type"] == "link" and post.get("content_url") and _is_video_url(post["content_url"]):
        media_path = await asyncio.get_event_loop().run_in_executor(None, download_video, post["content_url"])
        if media_path:
            media_path = await asyncio.get_event_loop().run_in_executor(None, compress_video, media_path)

    try:
        msg_id = await publish_post(config, post, media_path=media_path, media_paths=media_paths)
    except Exception as e:
        logger.warning("Failed to publish post %s: %s", post["reddit_id"], e)
        msg_id = None

    if msg_id:
        await mark_as_published(post["reddit_id"], msg_id)
        asyncio.create_task(_publish_comments_delayed(config, post, msg_id))

    if media_path:
        cleanup(media_path)
    if media_paths:
        for p in media_paths:
            cleanup(p)

    return bool(msg_id)


async def main() -> None:
    config = load_config()
    await init_db()

    stop_event = asyncio.Event()

    def _handle_signal(*_):
        logger.info("Shutdown signal received")
        stop_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal)

    logger.info(
        "Bot started — publish every %.0fs, scrape every %ds", config.pause_between_posts, config.scrape_interval
    )

    last_scrape: float = 0.0

    while not stop_event.is_set():
        now = time.monotonic()

        # Scrape if due
        if now - last_scrape >= config.scrape_interval:
            await scrape_new_posts(config)
            last_scrape = time.monotonic()

        # Publish one post
        # Publish one post (with timeout to prevent hanging on media download)
        try:
            await asyncio.wait_for(publish_one(config), timeout=300)
        except TimeoutError:
            logger.warning("publish_one timed out after 5 minutes")

        # Wait before next publish tick
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=config.pause_between_posts)

    logger.info("Bot stopped")


if __name__ == "__main__":
    asyncio.run(main())
