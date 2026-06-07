import asyncio
import html
import logging
import re
from datetime import UTC, datetime

import httpx
from bs4 import BeautifulSoup

from src.config import Config

logger = logging.getLogger(__name__)

# Reddit closed anonymous access to its .json API (403 from any IP), so we scrape
# the old.reddit.com HTML instead — it still serves listings/comments without auth.
OLD_REDDIT = "https://old.reddit.com"
# old.reddit blocks bot-looking User-Agents, so always present as a browser.
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
# Gallery images live in the post's gallery tiles. Selecting these classes scopes
# extraction to the actual gallery, excluding comment images, the post thumbnail, and
# "read next" suggestions that a whole-page scan would wrongly pull in.
_GALLERY_TILE_SELECTOR = "a.gallery-item-thumbnail-link[href], img.gallery-tile-content[src]"


def _headers() -> dict:
    return {
        "User-Agent": BROWSER_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }


async def _get_with_retry(client: httpx.AsyncClient, url: str, params: dict, *, label: str) -> httpx.Response:
    """GET with retry/backoff on old.reddit's 403/429 rate-limiting."""
    for attempt in range(3):
        response = await client.get(url, params=params, headers=_headers())
        if response.status_code in (403, 429) and attempt < 2:
            retry_after = int(response.headers.get("Retry-After", (attempt + 1) * 5))
            logger.info("Reddit %d on %s, retrying in %ds", response.status_code, label, retry_after)
            await asyncio.sleep(retry_after)
            continue
        response.raise_for_status()
        return response
    return response


def _detect_post_type(content_url: str | None, domain: str = "") -> str:
    url = (content_url or "").lower()
    if "/gallery/" in url:
        return "gallery"
    if "v.redd.it" in url or domain == "v.redd.it":
        return "video"
    if url.endswith((".gif", ".gifv")) or "gfycat.com" in url or "redgifs.com" in url:
        return "gif"
    if "i.redd.it" in url or url.endswith((".jpg", ".jpeg", ".png", ".webp")):
        return "image"
    if domain.startswith("self."):
        return "text"
    return "link"


def _abs_url(url: str | None) -> str | None:
    if not url:
        return None
    if url.startswith("//"):
        return "https:" + url
    return url


def _parse_thing(thing) -> dict | None:
    """Parse one old.reddit ``div.thing`` link element into a post dict."""
    if thing.get("data-promoted") == "true":
        return None
    fullname = thing.get("data-fullname", "")
    if not fullname.startswith("t3_"):
        return None
    author = thing.get("data-author") or "[deleted]"
    if author == "[deleted]":
        return None

    content_url = thing.get("data-url")
    domain = thing.get("data-domain", "")
    permalink = thing.get("data-permalink", "")
    post_type = _detect_post_type(content_url, domain)

    title_tag = thing.select_one("a.title")
    title = title_tag.get_text(strip=True) if title_tag else ""

    ts = thing.get("data-timestamp")
    created = datetime.fromtimestamp(int(ts) / 1000, tz=UTC) if ts else datetime.now(UTC)

    video_url = hls_url = None
    if post_type == "video" and content_url:
        vid = content_url.rstrip("/").rsplit("/", 1)[-1]
        hls_url = f"https://v.redd.it/{vid}/HLSPlaylist.m3u8"
        video_url = f"https://v.redd.it/{vid}/DASH_720.mp4"

    selftext = None
    if post_type == "text":
        md = thing.select_one("div.usertext-body div.md")
        if md:
            selftext = md.get_text("\n", strip=True) or None

    preview_url = None
    thumb = thing.select_one("a.thumbnail img")
    if thumb:
        src = _abs_url(thumb.get("src"))
        if src and src.startswith("http"):
            preview_url = src

    return {
        "reddit_id": fullname,
        "subreddit": thing.get("data-subreddit", ""),
        "title": title,
        "author": author,
        "url": f"https://reddit.com{permalink}",
        "content_url": content_url,
        "selftext": selftext,
        "score": int(thing.get("data-score") or 0),
        "num_comments": int(thing.get("data-comments-count") or 0),
        "post_type": post_type,
        "is_nsfw": thing.get("data-nsfw") == "true",
        "media_urls": None,  # filled in for galleries via _fetch_gallery_images
        "created_utc": created.isoformat(),
        "preview_url": preview_url,
        "video_url": video_url,
        "hls_url": hls_url,
    }


def _select_gallery_urls(page_html: str) -> list[str] | None:
    """Pick the gallery's image URLs from a post page's HTML.

    Each tile exposes the image at several widths via its link and thumbnail; preview.redd.it
    needs its `s=` signature (unsigned variants 403), so keep the largest signed variant per image.
    """
    soup = BeautifulSoup(page_html, "html.parser")
    order: list[str] = []
    best: dict[str, tuple[int, str]] = {}
    for el in soup.select(_GALLERY_TILE_SELECTOR):
        raw = el.get("href") or el.get("src")
        if not raw:
            continue
        url = html.unescape(raw)
        if "preview.redd.it" in url and not re.search(r"[?&]s=", url):
            continue
        path = url.split("?", 1)[0]
        width_match = re.search(r"[?&]width=(\d+)", url)
        width = int(width_match.group(1)) if width_match else 0
        if path not in best:
            order.append(path)
            best[path] = (width, url)
        elif width > best[path][0]:
            best[path] = (width, url)
    return [best[path][1] for path in order][:20] or None


async def _fetch_gallery_images(client: httpx.AsyncClient, post: dict) -> list[str] | None:
    """Fetch a gallery post's page and extract its image URLs."""
    permalink = post["url"].removeprefix("https://reddit.com")
    try:
        response = await _get_with_retry(client, f"{OLD_REDDIT}{permalink}", {}, label=f"gallery {post['reddit_id']}")
    except Exception:
        logger.warning("Failed to fetch gallery images for %s", post["reddit_id"])
        return None
    return _select_gallery_urls(response.text)


async def fetch_top_posts(config: Config) -> list[dict]:
    url = f"{OLD_REDDIT}/top/"
    params = {"t": "day", "limit": config.posts_limit}

    async with httpx.AsyncClient(timeout=30, follow_redirects=True, http2=True) as client:
        response = await _get_with_retry(client, url, params, label="top posts")
        soup = BeautifulSoup(response.text, "html.parser")
        posts = [post for thing in soup.select("div.thing") if (post := _parse_thing(thing))]

        for post in posts:
            if post["post_type"] == "gallery":
                # Space out per-post page fetches so old.reddit doesn't rate-limit the burst.
                await asyncio.sleep(2)
                post["media_urls"] = await _fetch_gallery_images(client, post)

    logger.info("Fetched %d posts from Reddit", len(posts))
    return posts


def _parse_comment_score(thing) -> int:
    score_tag = thing.select_one("span.score.unvoted")
    if score_tag and score_tag.get("title"):
        try:
            return int(score_tag["title"].split()[0])
        except (ValueError, IndexError):
            pass
    return 0


def _extract_comment_media(md_tag) -> tuple[str | None, str | None]:
    """Pull an image/gif URL out of a comment body's rendered HTML.

    old.reddit renders a gif/emote as ``<img src=...>`` and a Reddit-uploaded image as
    ``<a href=...><image></a>``. ``.get_text()`` loses both, so read the URLs directly.
    """
    img = md_tag.find("img")
    if img and img.get("src"):
        return html.unescape(img["src"]), "gif"
    for anchor in md_tag.find_all("a", href=True):
        if anchor.get_text(strip=True) == "<image>":
            href = html.unescape(anchor["href"])
            return href, ("gif" if ".gif" in href.lower() else "image")
    return None, None


def _clean_comment_text(md_tag) -> str:
    """Visible comment text with the literal ``<image>`` media placeholder removed."""
    return md_tag.get_text("\n", strip=True).replace("<image>", "").strip()


async def fetch_top_comments(config: Config, post: dict, limit: int = 5) -> list[dict]:
    """Fetch top-level comments sorted by score from old.reddit HTML."""
    permalink = post["url"].removeprefix("https://reddit.com")
    url = f"{OLD_REDDIT}{permalink}"
    params = {"sort": "top", "limit": 50}

    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True, http2=True) as client:
            response = await _get_with_retry(client, url, params, label=f"comments {post['reddit_id']}")

        soup = BeautifulSoup(response.text, "html.parser")
        comments = []
        # Direct children of the main comment listing = top-level comments only.
        for thing in soup.select("div.commentarea > div.sitetable > div.comment"):
            classes = thing.get("class", [])
            if "stickied" in classes or "deleted" in classes:
                continue
            author = thing.get("data-author")
            if not author or author == "[deleted]":
                continue
            body_tag = thing.select_one("div.entry div.usertext-body div.md")
            if not body_tag:
                continue
            media_url, media_type = _extract_comment_media(body_tag)
            body = _clean_comment_text(body_tag)
            if not media_url and body in ("", "[removed]", "[deleted]"):
                continue
            comments.append(
                {
                    "author": author,
                    "body": body,
                    "score": _parse_comment_score(thing),
                    "media_url": media_url,
                    "media_type": media_type,
                }
            )

        comments.sort(key=lambda x: x["score"], reverse=True)
        comments = comments[:limit]
        logger.info("Fetched %d top comments for %s", len(comments), post["reddit_id"])
        return comments
    except Exception:
        logger.warning("Failed to fetch comments for %s", post["reddit_id"], exc_info=True)
        return []


async def fetch_fresh_hls_url(config: Config, reddit_id: str) -> str | None:
    """Kept for API compatibility.

    The HLS URL is now a static path derived at parse time (no expiring token),
    so there is nothing to refresh — callers fall back to the stored hls_url.
    """
    return None
