from pathlib import Path

import httpx
import respx
from httpx import Response

from src.config import Config
from src.scraper.reddit import (
    _fetch_gallery_images,
    fetch_fresh_hls_url,
    fetch_top_comments,
    fetch_top_posts,
)

LISTING_HTML = Path("tests/fixtures/old_reddit_top.html").read_text()
COMMENTS_HTML = Path("tests/fixtures/old_reddit_comments.html").read_text()

CONFIG = Config(telegram_bot_token="test", telegram_chat_id="test", database_url="postgresql://test", posts_limit=25)

SAMPLE_POST = {
    "reddit_id": "t3_abc123",
    "subreddit": "funny",
    "url": "https://reddit.com/r/funny/comments/abc123/x/",
}
COMMENTS_URL = "https://old.reddit.com/r/funny/comments/abc123/x/"


# --- fetch_top_posts ---


@respx.mock
async def test_fetch_top_posts_skips_promoted_and_deleted():
    respx.get("https://old.reddit.com/top/").mock(return_value=Response(200, html=LISTING_HTML))
    posts = await fetch_top_posts(CONFIG)
    ids = {p["reddit_id"] for p in posts}
    assert "t3_ad1" not in ids
    assert "t3_del1" not in ids


# --- fetch_top_comments ---


@respx.mock
async def test_fetch_top_comments_parses_and_sorts():
    respx.get(COMMENTS_URL).mock(return_value=Response(200, html=COMMENTS_HTML))
    comments = await fetch_top_comments(CONFIG, SAMPLE_POST)
    assert [c["author"] for c in comments] == ["high", "low"]
    assert comments[0]["score"] == 999


@respx.mock
async def test_fetch_top_comments_filters_deleted_stickied_removed():
    respx.get(COMMENTS_URL).mock(return_value=Response(200, html=COMMENTS_HTML))
    comments = await fetch_top_comments(CONFIG, SAMPLE_POST)
    authors = [c["author"] for c in comments]
    bodies = [c["body"] for c in comments]
    assert "[deleted]" not in authors
    assert "moderator" not in authors  # stickied
    assert "[removed]" not in bodies


@respx.mock
async def test_fetch_top_comments_ignores_nested_replies():
    respx.get(COMMENTS_URL).mock(return_value=Response(200, html=COMMENTS_HTML))
    comments = await fetch_top_comments(CONFIG, SAMPLE_POST)
    assert "nestedguy" not in [c["author"] for c in comments]


@respx.mock
async def test_fetch_top_comments_returns_empty_on_error():
    respx.get(COMMENTS_URL).mock(return_value=Response(500))
    assert await fetch_top_comments(CONFIG, SAMPLE_POST) == []


# --- _fetch_gallery_images ---


@respx.mock
async def test_fetch_gallery_images_picks_largest_signed_variant():
    # Unsigned thumbnail (no s=) is skipped; the largest signed width wins even when a
    # smaller signed variant appears first; &amp; decoded; i.redd.it kept without a signature.
    page_html = (
        '<a href="https://preview.redd.it/aaa.jpg?width=108&amp;s=SIG_SMALL">small</a>'
        '<a href="https://preview.redd.it/aaa.jpg?width=140&amp;auto=webp">unsigned thumb</a>'
        '<a href="https://preview.redd.it/aaa.jpg?width=1170&amp;s=SIG_BIG">big</a>'
        " text https://preview.redd.it/bbb.png?s=SIG3 more"
        '<a href="https://i.redd.it/ccc.jpg">direct</a>'
    )
    respx.get(COMMENTS_URL).mock(return_value=Response(200, html=page_html))
    async with httpx.AsyncClient() as client:
        urls = await _fetch_gallery_images(client, SAMPLE_POST)
    assert urls == [
        "https://preview.redd.it/aaa.jpg?width=1170&s=SIG_BIG",
        "https://preview.redd.it/bbb.png?s=SIG3",
        "https://i.redd.it/ccc.jpg",
    ]


@respx.mock
async def test_fetch_gallery_images_none_on_error():
    respx.get(COMMENTS_URL).mock(return_value=Response(500))
    async with httpx.AsyncClient() as client:
        assert await _fetch_gallery_images(client, SAMPLE_POST) is None


# --- fetch_fresh_hls_url ---


async def test_fetch_fresh_hls_url_returns_none():
    # HLS is now a static derived path, so there is nothing to refresh.
    assert await fetch_fresh_hls_url(CONFIG, "t3_abc123") is None
