import logging
from datetime import UTC, datetime

import httpx

from src.config import Config

logger = logging.getLogger(__name__)

REDDIT_URL = "https://www.reddit.com/.json"


def _detect_post_type(data: dict) -> str:
    if data.get("is_gallery"):
        return "gallery"
    if data.get("is_video"):
        return "video"
    url = data.get("url", "")
    if data.get("post_hint") == "image" or url.startswith("https://i.redd.it"):
        return "image"
    if data.get("selftext"):
        return "text"
    return "link"


def _extract_media_urls(data: dict) -> list[str] | None:
    if not data.get("is_gallery"):
        return None
    items = data.get("gallery_data", {}).get("items", [])
    metadata = data.get("media_metadata", {})
    urls = []
    for item in items:
        media_id = item.get("media_id")
        if media_id and media_id in metadata:
            url = metadata[media_id].get("s", {}).get("u", "")
            if url:
                urls.append(url.replace("&amp;", "&"))
    return urls or None


def _parse_post(data: dict) -> dict:
    post_type = _detect_post_type(data)
    return {
        "reddit_id": f"t3_{data['id']}",
        "subreddit": data["subreddit"],
        "title": data["title"],
        "author": data.get("author", "[deleted]"),
        "url": f"https://reddit.com{data['permalink']}",
        "content_url": data.get("url"),
        "selftext": data.get("selftext") or None,
        "score": data.get("score", 0),
        "num_comments": data.get("num_comments", 0),
        "post_type": post_type,
        "is_nsfw": data.get("over_18", False),
        "media_urls": _extract_media_urls(data),
        "created_utc": datetime.fromtimestamp(data.get("created_utc", 0), tz=UTC).isoformat(),
    }


async def fetch_top_posts(config: Config) -> list[dict]:
    params = {"limit": config.posts_limit, "raw_json": 1, "sort": "top"}
    headers = {"User-Agent": config.reddit_user_agent}

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(REDDIT_URL, params=params, headers=headers)
        response.raise_for_status()

    children = response.json().get("data", {}).get("children", [])
    posts = [_parse_post(child["data"]) for child in children]
    logger.info("Fetched %d posts from Reddit", len(posts))
    return posts
