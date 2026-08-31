import asyncio
import html
import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup, NavigableString, Tag

from src.config import Config

logger = logging.getLogger(__name__)

# Reddit closed anonymous access to its .json API in June 2026, and by August 2026 old.reddit.com
# followed: every logged-out request there 302s to /login/?reason=lor2. www.reddit.com serves a JS
# challenge instead of HTML. The Atom feeds under www.reddit.com/<path>/.rss are the one surface
# still open without auth, so that is what we scrape.
REDDIT = "https://www.reddit.com"
# old.reddit blocks bot-looking User-Agents, so always present as a browser.
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

_ATOM = {"a": "http://www.w3.org/2005/Atom", "media": "http://search.yahoo.com/mrss/"}

# Measured 2026-08-31: the feeds are rate-limited per IP in bursts, with no Retry-After header.
# One request per 60s ran 13/13 clean; 30s spacing alternated 200/429 and 15s spacing failed 4 of 5.
# Every Reddit request in this module goes through _rss_get, which serialises on this interval.
_MIN_REQUEST_INTERVAL = 60.0
_RATE_LIMIT_LOCK = asyncio.Lock()
_last_request_at = 0.0


def _headers() -> dict:
    return {
        "User-Agent": BROWSER_UA,
        "Accept": "application/atom+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }


async def _rss_get(client: httpx.AsyncClient, url: str, params: dict, *, label: str) -> httpx.Response:
    """GET a Reddit feed, pacing requests globally and retrying the 403/429 rate-limit responses.

    The pacing lock is held for the whole request so two concurrent callers (an hourly scrape and
    a publish-time comment fetch) cannot interleave into a burst that trips the limiter.
    """
    global _last_request_at
    async with _RATE_LIMIT_LOCK:
        for attempt in range(3):
            wait = _MIN_REQUEST_INTERVAL - (time.monotonic() - _last_request_at)
            if wait > 0:
                await asyncio.sleep(wait)
            response = await client.get(url, params=params, headers=_headers())
            _last_request_at = time.monotonic()
            # Reddit sends no Retry-After on these, and a transient 403 clears on the next slot,
            # so just wait out another full interval instead of guessing a shorter backoff.
            if response.status_code in (403, 429) and attempt < 2:
                logger.info("Reddit %d on %s, retrying after %.0fs", response.status_code, label, _MIN_REQUEST_INTERVAL)
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


_INLINE_MD = {"strong": "**", "b": "**", "em": "*", "i": "*", "del": "~~", "s": "~~", "strike": "~~", "code": "`"}


def _html_to_markdown(node: Tag) -> str:
    """Turn a rendered ``div.md`` subtree back into Reddit markdown.

    The feed carries only the *rendered* HTML, but the Telegram publisher expects markdown (that is
    how bodies arrived from the old JSON API). Reconstructing markdown keeps paragraph breaks, links
    and emphasis so the existing markdown→Telegram pipeline formats bodies as it always did.
    """
    out: list[str] = []
    for child in node.children:
        if isinstance(child, NavigableString):
            out.append(str(child))
            continue
        if not isinstance(child, Tag):
            continue
        name = child.name
        inner = _html_to_markdown(child)
        if name == "p":
            out.append(inner.strip() + "\n\n")
        elif name in _INLINE_MD:
            marker = _INLINE_MD[name]
            out.append(f"{marker}{inner}{marker}")
        elif name == "a":
            out.append(f"[{inner}]({child.get('href', '')})")
        elif name == "br":
            out.append("\n")
        elif name in ("h1", "h2", "h3", "h4", "h5", "h6"):
            out.append(f"# {inner.strip()}\n\n")
        elif name == "blockquote":
            quoted = "\n".join(f"> {line}" for line in inner.strip().split("\n"))
            out.append(quoted + "\n\n")
        elif name == "li":
            out.append(f"- {inner.strip()}\n")
        elif name in ("ul", "ol"):
            out.append(inner + "\n")
        elif name == "pre":
            out.append(f"```\n{child.get_text().strip()}\n```\n\n")
        elif name == "hr":
            out.append("\n---\n\n")
        else:
            out.append(inner)
    return "".join(out)


def _selftext_from_md(md: Tag) -> str | None:
    """Extract a post body as markdown from its rendered ``div.md`` element."""
    text = re.sub(r"\n{3,}", "\n\n", _html_to_markdown(md)).strip()
    return text or None


def _entry_text(entry: ET.Element, path: str) -> str:
    return (entry.findtext(path, "", _ATOM) or "").strip()


def _entry_author(entry: ET.Element) -> str:
    """Author name without the ``/u/`` prefix the feed wraps it in."""
    return _entry_text(entry, "a:author/a:name").removeprefix("/u/").strip()


def _entry_permalink(entry: ET.Element) -> str:
    link = entry.find("a:link", _ATOM)
    href = (link.get("href") or "") if link is not None else ""
    return href.strip()


def _entry_created(entry: ET.Element) -> datetime:
    stamp = _entry_text(entry, "a:published") or _entry_text(entry, "a:updated")
    try:
        return datetime.fromisoformat(stamp).astimezone(UTC)
    except ValueError:
        return datetime.now(UTC)


def _content_soup(entry: ET.Element) -> BeautifulSoup:
    """The entry's ``content`` payload — an HTML fragment — parsed for links, body and thumbnail.

    The payload is escaped twice: the XML parser strips the outer layer, leaving HTML whose own
    entities (``&lt;image&gt;`` placeholders, ``&amp;`` inside URLs) belong to the HTML parser.
    Unescaping here as well would turn the ``<image>`` placeholder into a tag and lose it.
    """
    return BeautifulSoup(_entry_text(entry, "a:content"), "html.parser")


def _linked_url(soup: BeautifulSoup, label: str) -> str | None:
    """Read one of the feed's trailing ``[link]`` / ``[comments]`` anchors."""
    for anchor in soup.find_all("a", href=True):
        if anchor.get_text(strip=True) == label:
            return html.unescape(anchor["href"])
    return None


def _thumbnail(entry: ET.Element, soup: BeautifulSoup) -> str | None:
    """Preview image for the post — a ~320px signed render, the largest the feed exposes."""
    media = entry.find("media:thumbnail", _ATOM)
    if media is not None and (media.get("url") or "").startswith("http"):
        return html.unescape(media.get("url"))
    img = soup.find("img", src=True)
    if img and img["src"].startswith("http"):
        return html.unescape(img["src"])
    return None


def _parse_entry(entry: ET.Element, rank: int, total: int) -> dict | None:
    """Parse one Atom ``entry`` from a listing feed into a post dict."""
    reddit_id = _entry_text(entry, "a:id")
    if not reddit_id.startswith("t3_"):
        return None
    author = _entry_author(entry)
    if not author or author == "[deleted]":
        return None

    permalink = _entry_permalink(entry)
    if not permalink:
        return None
    path = urlparse(permalink).path

    soup = _content_soup(entry)
    content_url = _linked_url(soup, "[link]") or permalink
    # A self post's [link] anchor points back at the post itself; _detect_post_type keys off the
    # old JSON API's "self.<subreddit>" domain convention, so reproduce it here.
    category = entry.find("a:category", _ATOM)
    subreddit = (category.get("label") or category.get("term") or "").removeprefix("r/") if category is not None else ""
    is_self = urlparse(content_url).path == path
    domain = f"self.{subreddit}" if is_self else urlparse(content_url).netloc

    post_type = _detect_post_type(None if is_self else content_url, domain)

    video_url = hls_url = None
    if post_type == "video":
        vid = content_url.rstrip("/").rsplit("/", 1)[-1]
        hls_url = f"https://v.redd.it/{vid}/HLSPlaylist.m3u8"
        video_url = f"https://v.redd.it/{vid}/DASH_720.mp4"

    if post_type == "gallery":
        # The feed exposes a gallery's cover thumbnail only — the tiles lived on the post page,
        # which is no longer reachable. Publishing the cover plus the gallery link beats dropping
        # the post, and that is exactly how a link post is already handled downstream.
        post_type = "link"

    md = soup.select_one("div.md")
    return {
        "reddit_id": reddit_id,
        "subreddit": subreddit,
        "title": _entry_text(entry, "a:title"),
        "author": author,
        "url": f"https://reddit.com{path}",
        "content_url": None if is_self else content_url,
        "selftext": _selftext_from_md(md) if md else None,
        # The feed carries no score. Posts arrive in Reddit's own top order, so rank stands in for
        # it: the publisher's "highest score first" queue then preserves that order.
        "score": max(total - rank, 1),
        "num_comments": 0,  # not exposed by the feed
        "post_type": post_type,
        # Not exposed by the feed either. The logged-out r/all listing already excludes NSFW posts,
        # so nothing NSFW reaches us to flag.
        "is_nsfw": False,
        "media_urls": None,
        "created_utc": _entry_created(entry).isoformat(),
        "preview_url": _thumbnail(entry, soup),
        "video_url": video_url,
        "hls_url": hls_url,
    }


def _entries(xml: bytes) -> list[ET.Element]:
    try:
        return ET.fromstring(xml).findall("a:entry", _ATOM)
    except ET.ParseError:
        logger.warning("Reddit returned unparseable XML")
        return []


async def fetch_top_posts(config: Config) -> list[dict]:
    """Fetch the day's top posts from the r/all Atom feed.

    One request returns up to 100 entries carrying everything the publisher needs — body, media
    link and preview — so unlike the old HTML scraper there is no per-post follow-up fetch.
    """
    url = f"{REDDIT}/r/all/top/.rss"
    params = {"t": "day", "limit": config.posts_limit}

    async with httpx.AsyncClient(timeout=30, follow_redirects=True, http2=True) as client:
        response = await _rss_get(client, url, params, label="top posts")

    entries = _entries(response.content)
    posts = [post for i, e in enumerate(entries) if (post := _parse_entry(e, i, len(entries)))]
    logger.info("Fetched %d posts from Reddit", len(posts))
    return posts


def _extract_comment_media(md_tag) -> tuple[str | None, str | None]:
    """Pull an image/gif URL out of a comment body's rendered HTML.

    Reddit renders a gif/emote as ``<img src=...>`` and an uploaded image as
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
    """Fetch a post's comments from its own Atom feed.

    The feed has no comment scores, so it is taken in feed order — Reddit sorts it the way the
    post page does, and the first entries are the ones a reader sees at the top.
    """
    permalink = post["url"].removeprefix("https://reddit.com")
    url = f"{REDDIT}{permalink.rstrip('/')}/.rss"
    params = {"sort": "top", "limit": 50}

    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True, http2=True) as client:
            response = await _rss_get(client, url, params, label=f"comments {post['reddit_id']}")

        comments = []
        for entry in _entries(response.content):
            # The feed leads with the post itself (t3_); only t1_ entries are comments.
            if not _entry_text(entry, "a:id").startswith("t1_"):
                continue
            author = _entry_author(entry)
            if not author or author == "[deleted]":
                continue
            body_tag = _content_soup(entry).select_one("div.md")
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
                    "score": 0,  # not exposed by the feed
                    "media_url": media_url,
                    "media_type": media_type,
                }
            )
            if len(comments) == limit:
                break

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
