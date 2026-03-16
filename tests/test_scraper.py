import json
from pathlib import Path

import respx
from httpx import Response

from src.config import Config
from src.scraper.reddit import _detect_post_type, _extract_media_urls, _parse_post, fetch_top_posts

FIXTURE = json.loads((Path("tests/fixtures/reddit_top.json")).read_text())
POSTS_DATA = [child["data"] for child in FIXTURE["data"]["children"]]

CONFIG = Config(telegram_bot_token="test", telegram_chat_id="test")


@respx.mock
async def test_fetch_top_posts_returns_list():
    respx.get("https://www.reddit.com/.json").mock(return_value=Response(200, json=FIXTURE))
    posts = await fetch_top_posts(CONFIG)
    assert len(posts) == 6


@respx.mock
async def test_fetch_top_posts_reddit_id_format():
    respx.get("https://www.reddit.com/.json").mock(return_value=Response(200, json=FIXTURE))
    posts = await fetch_top_posts(CONFIG)
    assert posts[0]["reddit_id"] == "t3_abc123"


def test_detect_image_post():
    assert _detect_post_type(POSTS_DATA[0]) == "image"


def test_detect_video_post():
    assert _detect_post_type(POSTS_DATA[1]) == "video"


def test_detect_text_post():
    assert _detect_post_type(POSTS_DATA[2]) == "text"


def test_detect_gallery_post():
    assert _detect_post_type(POSTS_DATA[4]) == "gallery"


def test_detect_link_post():
    assert _detect_post_type(POSTS_DATA[5]) == "link"


def test_parse_nsfw_flag():
    post = _parse_post(POSTS_DATA[3])
    assert post["is_nsfw"] is True


def test_parse_normal_post_not_nsfw():
    post = _parse_post(POSTS_DATA[0])
    assert post["is_nsfw"] is False


def test_extract_gallery_urls():
    urls = _extract_media_urls(POSTS_DATA[4])
    assert urls == ["https://i.redd.it/img1.jpg", "https://i.redd.it/img2.jpg"]


def test_extract_media_urls_non_gallery():
    assert _extract_media_urls(POSTS_DATA[0]) is None


def test_parse_selftext_none_when_empty():
    post = _parse_post(POSTS_DATA[0])
    assert post["selftext"] is None


def test_parse_selftext_present():
    post = _parse_post(POSTS_DATA[2])
    assert post["selftext"] == "This is the body of the text post."
