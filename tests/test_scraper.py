from pathlib import Path

import respx
from bs4 import BeautifulSoup
from httpx import Response

from src.config import Config
from src.scraper.reddit import _detect_post_type, _parse_thing, fetch_top_posts

LISTING_HTML = Path("tests/fixtures/old_reddit_top.html").read_text()
SOUP = BeautifulSoup(LISTING_HTML, "html.parser")
THINGS = {t.get("data-fullname"): t for t in SOUP.select("div.thing")}

CONFIG = Config(telegram_bot_token="test", telegram_chat_id="test", database_url="postgresql://test", posts_limit=25)


@respx.mock
async def test_fetch_top_posts_returns_valid_posts():
    respx.get("https://old.reddit.com/top/").mock(return_value=Response(200, html=LISTING_HTML))
    posts = await fetch_top_posts(CONFIG)
    assert len(posts) == 5  # promoted ad and deleted-author post are skipped


@respx.mock
async def test_fetch_top_posts_reddit_id_format():
    respx.get("https://old.reddit.com/top/").mock(return_value=Response(200, html=LISTING_HTML))
    posts = await fetch_top_posts(CONFIG)
    assert all(p["reddit_id"].startswith("t3_") for p in posts)


def test_detect_image_post():
    assert _detect_post_type("https://i.redd.it/x.jpeg", "i.redd.it") == "image"


def test_detect_video_post():
    assert _detect_post_type("https://v.redd.it/x", "v.redd.it") == "video"


def test_detect_gif_post():
    assert _detect_post_type("https://i.redd.it/x.gif", "i.redd.it") == "gif"


def test_detect_gallery_post():
    assert _detect_post_type("https://www.reddit.com/gallery/x", "reddit.com") == "gallery"


def test_detect_text_post():
    assert _detect_post_type("https://www.reddit.com/r/s/comments/x/", "self.s") == "text"


def test_detect_link_post():
    assert _detect_post_type("https://example.com/a", "example.com") == "link"


def test_parse_image_post():
    post = _parse_thing(THINGS["t3_img1"])
    assert post["post_type"] == "image"
    assert post["content_url"] == "https://i.redd.it/abc.jpeg"
    assert post["url"] == "https://reddit.com/r/pics/comments/img1/a_cat/"


def test_parse_score_and_comments():
    post = _parse_thing(THINGS["t3_img1"])
    assert post["score"] == 1234
    assert post["num_comments"] == 56


def test_parse_normal_post_not_nsfw():
    assert _parse_thing(THINGS["t3_img1"])["is_nsfw"] is False


def test_parse_nsfw_flag():
    assert _parse_thing(THINGS["t3_nsfw1"])["is_nsfw"] is True


def test_parse_selftext_present():
    assert _parse_thing(THINGS["t3_text1"])["selftext"] == "This is the body of the text post."


def test_parse_selftext_none_when_image():
    assert _parse_thing(THINGS["t3_img1"])["selftext"] is None


def test_parse_video_derives_hls_and_fallback():
    post = _parse_thing(THINGS["t3_vid1"])
    assert post["post_type"] == "video"
    assert post["hls_url"] == "https://v.redd.it/vid123/HLSPlaylist.m3u8"
    assert post["video_url"] == "https://v.redd.it/vid123/DASH_720.mp4"


def test_parse_skips_promoted():
    assert _parse_thing(THINGS["t3_ad1"]) is None


def test_parse_skips_deleted_author():
    assert _parse_thing(THINGS["t3_del1"]) is None
