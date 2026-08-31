from pathlib import Path

import pytest
import respx
from httpx import Response

from src.config import Config
from src.scraper.reddit import _detect_post_type, fetch_top_posts

LISTING_RSS = Path("tests/fixtures/reddit_top.rss").read_bytes()
LISTING_URL = "https://www.reddit.com/r/all/top/.rss"

CONFIG = Config(telegram_bot_token="test", telegram_chat_id="test", database_url="postgresql://test", posts_limit=25)


@pytest.fixture
async def posts():
    with respx.mock:
        respx.get(LISTING_URL).mock(return_value=Response(200, content=LISTING_RSS))
        return {p["reddit_id"]: p for p in await fetch_top_posts(CONFIG)}


async def test_fetch_top_posts_returns_valid_posts(posts):
    assert len(posts) == 5  # the deleted-author entry is skipped


async def test_fetch_top_posts_reddit_id_format(posts):
    assert all(rid.startswith("t3_") for rid in posts)


async def test_fetch_top_posts_skips_deleted_author(posts):
    assert "t3_del1" not in posts


def test_detect_image_post():
    assert _detect_post_type("https://i.redd.it/x.jpeg", "i.redd.it") == "image"


def test_detect_video_post():
    assert _detect_post_type("https://v.redd.it/x", "v.redd.it") == "video"


def test_detect_gif_post():
    assert _detect_post_type("https://i.redd.it/x.gif", "i.redd.it") == "gif"


def test_detect_gallery_post():
    assert _detect_post_type("https://www.reddit.com/gallery/x", "reddit.com") == "gallery"


def test_detect_text_post():
    assert _detect_post_type(None, "self.s") == "text"


def test_detect_link_post():
    assert _detect_post_type("https://example.com/a", "example.com") == "link"


async def test_parse_image_post(posts):
    post = posts["t3_img1"]
    assert post["post_type"] == "image"
    assert post["content_url"] == "https://i.redd.it/abc.jpeg"
    assert post["url"] == "https://reddit.com/r/pics/comments/img1/a_cat/"


async def test_parse_reads_subreddit_and_author(posts):
    post = posts["t3_img1"]
    assert post["subreddit"] == "pics"
    assert post["author"] == "photographer"  # the feed's /u/ prefix is stripped


async def test_parse_score_ranks_by_feed_order(posts):
    """The feed carries no score, so rank stands in for it — earlier entries must sort first."""
    assert posts["t3_img1"]["score"] > posts["t3_vid1"]["score"] > posts["t3_lnk1"]["score"]


async def test_parse_num_comments_absent_from_feed(posts):
    assert posts["t3_img1"]["num_comments"] == 0


async def test_parse_nsfw_always_false(posts):
    # The logged-out r/all feed excludes NSFW posts, and exposes no flag for them either.
    assert posts["t3_img1"]["is_nsfw"] is False


async def test_parse_created_utc_from_published(posts):
    assert posts["t3_img1"]["created_utc"].startswith("2026-08-30T13:30:26")


async def test_parse_selftext_present(posts):
    assert posts["t3_text1"]["selftext"] == "This is the body of the text post."


async def test_parse_self_post_type_and_no_content_url(posts):
    post = posts["t3_text1"]
    assert post["post_type"] == "text"
    assert post["content_url"] is None


async def test_parse_selftext_none_when_image(posts):
    assert posts["t3_img1"]["selftext"] is None


async def test_parse_video_derives_hls_and_fallback(posts):
    post = posts["t3_vid1"]
    assert post["post_type"] == "video"
    assert post["hls_url"] == "https://v.redd.it/vid123/HLSPlaylist.m3u8"
    assert post["video_url"] == "https://v.redd.it/vid123/DASH_720.mp4"


async def test_parse_link_post_keeps_external_url_and_preview(posts):
    post = posts["t3_lnk1"]
    assert post["post_type"] == "link"
    assert post["content_url"] == "https://example.com/article"
    assert post["preview_url"] == "https://external-preview.redd.it/n.jpg?width=320&s=sig4"


async def test_parse_gallery_degrades_to_link(posts):
    """Gallery tiles lived on the post page, which is gone — publish the cover and the link."""
    post = posts["t3_gal1"]
    assert post["post_type"] == "link"
    assert post["content_url"] == "https://www.reddit.com/gallery/gal1"
    assert post["preview_url"] == "https://preview.redd.it/g.jpg?width=320&s=sig3"
    assert post["media_urls"] is None


@respx.mock
async def test_fetch_top_posts_empty_on_unparseable_body():
    respx.get(LISTING_URL).mock(return_value=Response(200, content=b"<html>login</html>"))
    assert await fetch_top_posts(CONFIG) == []


@respx.mock
async def test_fetch_top_posts_retries_rate_limit_then_succeeds():
    route = respx.get(LISTING_URL).mock(
        side_effect=[Response(429), Response(200, content=LISTING_RSS)],
    )
    posts = await fetch_top_posts(CONFIG)
    assert route.call_count == 2
    assert len(posts) == 5
