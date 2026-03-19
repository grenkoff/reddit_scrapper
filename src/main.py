import asyncio
import contextlib
import logging
import signal
from datetime import UTC, datetime

from src.config import load_config
from src.db import get_unpublished_posts, init_db, insert_post, is_post_exists, log_scrape, mark_as_published
from src.publisher.telegram import publish_post
from src.scraper.media import cleanup, compress_video, download_gif, download_image, download_video, download_video_direct
from src.scraper.reddit import fetch_top_posts

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


async def scrape_and_publish(config) -> None:
    started_at = datetime.now(UTC)
    posts_found = posts_new = posts_published = 0
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

        unpublished = await get_unpublished_posts()
        for post in unpublished:
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
                posts_published += 1

            if media_path:
                cleanup(media_path)
            if media_paths:
                for p in media_paths:
                    cleanup(p)

    except Exception as e:
        error = str(e)
        logger.error("Scrape cycle failed: %s", e, exc_info=True)
    finally:
        await log_scrape(
            started_at=started_at,
            finished_at=datetime.now(UTC),
            posts_found=posts_found,
            posts_new=posts_new,
            posts_published=posts_published,
            error=error,
        )
        logger.info("Cycle done: found=%d new=%d published=%d", posts_found, posts_new, posts_published)


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

    logger.info("Bot started, scraping every %d seconds", config.scrape_interval)

    while not stop_event.is_set():
        await scrape_and_publish(config)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=config.scrape_interval)

    logger.info("Bot stopped")


if __name__ == "__main__":
    asyncio.run(main())
