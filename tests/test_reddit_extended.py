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
    scores = [c["score"] for c in comments]
    assert scores == sorted(scores, reverse=True)
    assert comments[0]["author"] == "high"
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


@respx.mock
async def test_fetch_top_comments_extracts_image_media():
    respx.get(COMMENTS_URL).mock(return_value=Response(200, html=COMMENTS_HTML))
    comments = await fetch_top_comments(CONFIG, SAMPLE_POST)
    img = next(c for c in comments if c["author"] == "imguser")
    assert img["media_type"] == "image"
    assert img["media_url"] == "https://preview.redd.it/img1.png?width=1170&s=SIGIMG"
    assert img["body"] == ""  # the literal <image> placeholder is stripped


@respx.mock
async def test_fetch_top_comments_extracts_gif_media():
    respx.get(COMMENTS_URL).mock(return_value=Response(200, html=COMMENTS_HTML))
    comments = await fetch_top_comments(CONFIG, SAMPLE_POST)
    gif = next(c for c in comments if c["author"] == "gifuser")
    assert gif["media_type"] == "gif"
    assert gif["media_url"] == "https://external-preview.redd.it/gif1.gif?width=200&s=SIGGIF"


@respx.mock
async def test_fetch_top_comments_media_with_text_kept():
    respx.get(COMMENTS_URL).mock(return_value=Response(200, html=COMMENTS_HTML))
    comments = await fetch_top_comments(CONFIG, SAMPLE_POST)
    mixed = next(c for c in comments if c["author"] == "mixeduser")
    assert mixed["media_url"] == "https://preview.redd.it/img2.jpeg?s=SIGMIX"
    assert mixed["body"] == "nice pic"


@respx.mock
async def test_fetch_top_comments_text_only_has_no_media():
    respx.get(COMMENTS_URL).mock(return_value=Response(200, html=COMMENTS_HTML))
    comments = await fetch_top_comments(CONFIG, SAMPLE_POST)
    high = next(c for c in comments if c["author"] == "high")
    assert high["media_url"] is None
    assert high["media_type"] is None


# --- _fetch_gallery_images ---


@respx.mock
async def test_fetch_gallery_images_scopes_to_tiles_and_picks_largest_signed():
    # Only the gallery tiles count: the largest signed width per image wins, unsigned variants
    # are skipped, and images from comments / "read next" / thumbnail must NOT leak in.
    page_html = (
        '<div class="sitetable linklisting"><div class="thing">'
        '<div class="media-preview-content">'
        '<a class="gallery-item-thumbnail-link" href="https://preview.redd.it/aaa.jpg?width=1170&amp;s=BIG">'
        '<img class="gallery-tile-content" src="https://preview.redd.it/aaa.jpg?width=108&amp;s=SMALL"/></a>'
        '<a class="gallery-item-thumbnail-link" href="https://preview.redd.it/unsigned.jpg?width=140">'
        '<img class="gallery-tile-content" src="https://preview.redd.it/bbb.jpg?width=960&amp;s=B"/></a>'
        "</div></div></div>"
        '<a class="thumbnail"><img src="https://preview.redd.it/THUMB.jpg?s=T"/></a>'
        '<div class="commentarea"><div class="sitetable"><div class="comment"><div class="md">'
        '<p><a href="https://preview.redd.it/COMMENT.jpg?s=CMT">&lt;image&gt;</a></p></div></div></div></div>'
    )
    respx.get(COMMENTS_URL).mock(return_value=Response(200, html=page_html))
    async with httpx.AsyncClient() as client:
        urls = await _fetch_gallery_images(client, SAMPLE_POST)
    assert urls == [
        "https://preview.redd.it/aaa.jpg?width=1170&s=BIG",
        "https://preview.redd.it/bbb.jpg?width=960&s=B",
    ]
    joined = " ".join(urls)
    assert "COMMENT.jpg" not in joined
    assert "THUMB.jpg" not in joined
    assert "unsigned.jpg" not in joined


@respx.mock
async def test_fetch_gallery_images_none_on_error():
    respx.get(COMMENTS_URL).mock(return_value=Response(500))
    async with httpx.AsyncClient() as client:
        assert await _fetch_gallery_images(client, SAMPLE_POST) is None


# --- fetch_fresh_hls_url ---


async def test_fetch_fresh_hls_url_returns_none():
    # HLS is now a static derived path, so there is nothing to refresh.
    assert await fetch_fresh_hls_url(CONFIG, "t3_abc123") is None
