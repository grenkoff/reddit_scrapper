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
    close_db,
    delete_stale_posts,
    get_unpublished_posts,
    init_db,
    insert_post,
    is_post_exists,
    log_scrape,
    mark_as_published,
)
from src.publisher.poller import UpdatePoller
from src.publisher.telegram import publish_comment, publish_failed_notice, publish_post
from src.scraper.media import (
    cleanup,
    compress_video,
    download_gif,
    download_image,
    download_video,
    download_video_direct,
)
from src.scraper.reddit import enrich_post, fetch_fresh_hls_url, fetch_top_comments, fetch_top_posts
from src.webapp.server import start_webapp_task

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

_VIDEO_DOMAINS = {"youtube.com", "youtu.be", "vimeo.com", "twitter.com", "x.com", "tiktok.com", "streamable.com"}

# When no fresh (<24h) post is available, fall back to the unpublished backlog within this
# window. Posts older than this are deleted on each scrape so the DB does not grow unbounded.
_STALE_POST_AGE_HOURS = 48


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
        async with httpx.AsyncClient(timeout=30, follow_redirects=True, http2=True) as client:
            for post in posts:
                if config.skip_nsfw and post["is_nsfw"]:
                    continue
                if await is_post_exists(post["reddit_id"]):
                    continue
                # New post: fetch its page once to recover body/gallery/preview the listing omits.
                # Space fetches out so old.reddit doesn't rate-limit the burst.
                await asyncio.sleep(2)
                await enrich_post(client, post)
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


async def _publish_comments_delayed(config, poller: "UpdatePoller", post: dict, msg_id: int) -> None:
    """Fetch top comments and publish them in discussion group over 10 minutes."""
    try:
        discussion_msg_id = await poller.wait_for_discussion_id(msg_id, wait_seconds=30.0)
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


async def _drop_with_notice(config, post: dict, reason: str) -> bool:
    """Mark a post handled but send a link-only notice so it isn't silently lost."""
    logger.warning("%s %s — sending link-only fallback", reason, post["reddit_id"])
    try:
        fallback_id = await publish_failed_notice(config, post)
    except Exception as e:
        logger.warning("Link-only fallback failed for %s: %s", post["reddit_id"], e)
        fallback_id = None
    await mark_as_published(post["reddit_id"], fallback_id or 0)
    return False


async def publish_one(config, poller: "UpdatePoller") -> bool | None:
    """Pick the next unpublished post and publish it.

    Returns True if published, False if skipped (publish failed), None if queue is empty.
    """
    posts = await get_unpublished_posts(limit=1)
    if not posts:
        # No fresh (<24h) post — fall back to the highest-scoring backlog post within the window.
        posts = await get_unpublished_posts(limit=1, max_age_hours=_STALE_POST_AGE_HOURS)
    if not posts:
        return None

    post = posts[0]
    media_path = None
    media_paths = None

    if post["post_type"] == "image" and post.get("content_url"):
        media_path = await download_image(post["content_url"])
        if media_path is None:
            return await _drop_with_notice(config, post, "Image download failed for")
    elif post["post_type"] == "gif" and post.get("content_url"):
        media_path = await download_gif(post["content_url"])
        if media_path is None:
            return await _drop_with_notice(config, post, "GIF download failed for")
    elif post["post_type"] == "video" and post.get("video_url"):
        fresh_hls = await fetch_fresh_hls_url(config, post["reddit_id"])
        hls_url = fresh_hls or post.get("hls_url")
        if not hls_url:
            logger.warning("No HLS URL for %s — video will have no audio", post["reddit_id"])
        media_path = await download_video_direct(post["video_url"], hls_url=hls_url)
        if media_path:
            media_path = await asyncio.get_event_loop().run_in_executor(None, compress_video, media_path)
        if media_path is None:
            return await _drop_with_notice(config, post, "Video download/compress failed for")
    elif post["post_type"] == "gallery" and post.get("media_urls"):
        paths = [await download_image(url) for url in post["media_urls"]]
        media_paths = [p for p in paths if p is not None] or None
        if media_paths is None:
            return await _drop_with_notice(config, post, "Gallery download failed for")
    elif post["post_type"] == "link" and post.get("content_url") and _is_video_url(post["content_url"]):
        media_path = await asyncio.get_event_loop().run_in_executor(None, download_video, post["content_url"])
        if media_path:
            media_path = await asyncio.get_event_loop().run_in_executor(None, compress_video, media_path)
        if media_path is None:
            return await _drop_with_notice(config, post, "Link-video download/compress failed for")

    try:
        msg_id = await publish_post(config, post, media_path=media_path, media_paths=media_paths)
    except Exception as e:
        logger.warning("Failed to publish post %s: %s", post["reddit_id"], e)
        msg_id = None

    if msg_id:
        await mark_as_published(post["reddit_id"], msg_id)
        poller.register_post(msg_id, post)
        asyncio.create_task(_publish_comments_delayed(config, poller, post, msg_id))
    else:
        # Publishing failed — send a link-only notice so the post isn't silently lost.
        await _drop_with_notice(config, post, "Publish failed for")

    if media_path:
        cleanup(media_path)
    if media_paths:
        for p in media_paths:
            cleanup(p)

    return bool(msg_id)  # True=published, False=skipped


async def _fetch_bot_username(token: str) -> str | None:
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            response = await client.get(f"https://api.telegram.org/bot{token}/getMe")
            if response.status_code == 200:
                return response.json()["result"].get("username")
        except Exception:
            logger.warning("Failed to fetch bot username", exc_info=True)
    return None


async def main() -> None:
    config = load_config()
    await init_db(config.database_url)

    config.bot_username = await _fetch_bot_username(config.telegram_bot_token)
    logger.info("Bot username: %s", config.bot_username)

    stop_event = asyncio.Event()

    def _handle_signal(*_):
        logger.info("Shutdown signal received")
        stop_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal)

    poller = UpdatePoller(config)
    asyncio.create_task(poller.run(stop_event))

    if config.gemini_api_key:
        start_webapp_task(config)
        logger.info("Web app started on port %d", config.webapp_port)

    logger.info(
        "Bot started — publish every %.0fs, scrape every %ds", config.pause_between_posts, config.scrape_interval
    )

    last_scrape: float = float("-inf")

    while not stop_event.is_set():
        now = time.monotonic()

        # Scrape if due
        if now - last_scrape >= config.scrape_interval:
            await scrape_new_posts(config)
            deleted = await delete_stale_posts(_STALE_POST_AGE_HOURS)
            if deleted:
                logger.info("Deleted %d stale unpublished posts (>%dh)", deleted, _STALE_POST_AGE_HOURS)
            last_scrape = time.monotonic()

        # Publish one post (with timeout to prevent hanging on media download)
        result = None
        try:
            result = await asyncio.wait_for(publish_one(config, poller), timeout=300)
        except TimeoutError:
            logger.warning("publish_one timed out after 5 minutes")
            posts = await get_unpublished_posts(limit=1)
            if posts:
                logger.warning("Skipping post %s due to timeout", posts[0]["reddit_id"])
                await mark_as_published(posts[0]["reddit_id"], 0)

        # Wait only after a successful publish or when the queue is empty.
        # After a skip (result is False) go straight to the next post.
        if result is not False:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=config.pause_between_posts)

    logger.info("Bot stopped")
    await close_db()


if __name__ == "__main__":
    asyncio.run(main())
