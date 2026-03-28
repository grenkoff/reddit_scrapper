import html as _html
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
    if data.get("post_hint") == "animated_image" or url.lower().endswith(".gif"):
        return "gif"
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
    raw_preview = data.get("preview", {}).get("images", [{}])[0].get("source", {}).get("url", "")
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
        "preview_url": _html.unescape(raw_preview) if raw_preview else None,
        "video_url": (
            (data.get("media") or {}).get("reddit_video", {}).get("fallback_url")
            or (data.get("secure_media") or {}).get("reddit_video", {}).get("fallback_url")
        )
        or None,
        "hls_url": (
            (data.get("media") or {}).get("reddit_video", {}).get("hls_url")
            or (data.get("secure_media") or {}).get("reddit_video", {}).get("hls_url")
        )
        or None,
    }


async def fetch_top_posts(config: Config) -> list[dict]:
    params = {"limit": config.posts_limit, "raw_json": 1, "sort": "top"}
    headers = {
        "User-Agent": config.reddit_user_agent,
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
    }

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        response = await client.get(REDDIT_URL, params=params, headers=headers)
        response.raise_for_status()

    children = response.json().get("data", {}).get("children", [])
    posts = []
    for child in children:
        d = child["data"]
        if d.get("removed_by_category") or d.get("selftext") == "[removed]":
            logger.debug("Skipping removed post %s", d.get("id"))
            continue
        posts.append(_parse_post(d))
    logger.info("Fetched %d posts from Reddit", len(posts))
    return posts


async def fetch_top_comments(config: Config, post: dict, limit: int = 5) -> list[dict]:
    """Fetch top-level comments sorted by score."""
    reddit_id = post["reddit_id"].removeprefix("t3_")
    url = f"https://www.reddit.com/r/{post['subreddit']}/comments/{reddit_id}.json"
    params = {"raw_json": 1, "sort": "top", "limit": 20}
    headers = {
        "User-Agent": config.reddit_user_agent,
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
    }

    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            response = await client.get(url, params=params, headers=headers)
            response.raise_for_status()

        data = response.json()
        if not isinstance(data, list) or len(data) < 2:
            return []

        children = data[1].get("data", {}).get("children", [])
        comments = []
        for child in children:
            if child.get("kind") != "t1":
                continue
            c = child["data"]
            if c.get("stickied"):
                continue
            if c.get("author") in ("[deleted]", None) or c.get("body") in ("[removed]", "[deleted]", ""):
                continue
            comments.append(
                {
                    "author": c.get("author", "[deleted]"),
                    "body": c.get("body", ""),
                    "score": c.get("score", 0),
                }
            )

        comments.sort(key=lambda x: x["score"], reverse=True)
        comments = comments[:limit]
        logger.info("Fetched %d top comments for %s", len(comments), post["reddit_id"])
        return comments
    except Exception:
        logger.warning("Failed to fetch comments for %s", post["reddit_id"], exc_info=True)
        return []
