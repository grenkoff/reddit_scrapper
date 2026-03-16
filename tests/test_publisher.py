import respx
from httpx import Response

from src.config import Config
from src.publisher.telegram import _build_caption, publish_post

CONFIG = Config(telegram_bot_token="testtoken", telegram_chat_id="-100123456", pause_between_posts=0)

BASE_POST = {
    "reddit_id": "t3_abc123",
    "subreddit": "programming",
    "title": "Test post title",
    "author": "user1",
    "url": "https://reddit.com/r/programming/comments/abc123",
    "selftext": None,
    "score": 5000,
    "num_comments": 200,
    "post_type": "text",
    "is_nsfw": False,
    "media_urls": None,
}


def test_caption_contains_title():
    caption = _build_caption(BASE_POST)
    assert "Test post title" in caption


def test_caption_contains_score():
    caption = _build_caption(BASE_POST)
    assert "5000" in caption


def test_caption_contains_subreddit():
    caption = _build_caption(BASE_POST)
    assert "r/programming" in caption


def test_caption_contains_url():
    caption = _build_caption(BASE_POST)
    assert BASE_POST["url"] in caption


def test_caption_selftext_truncated():
    post = {**BASE_POST, "selftext": "x" * 600}
    caption = _build_caption(post)
    assert "..." in caption


def test_caption_max_length():
    post = {**BASE_POST, "title": "T" * 500, "selftext": "S" * 600}
    caption = _build_caption(post)
    assert len(caption) <= 1024


@respx.mock
async def test_publish_text_post_returns_message_id():
    respx.post("https://api.telegram.org/bottesttoken/sendMessage").mock(
        return_value=Response(200, json={"result": {"message_id": 42}})
    )
    msg_id = await publish_post(CONFIG, BASE_POST)
    assert msg_id == 42


@respx.mock
async def test_publish_text_post_on_api_failure():
    respx.post("https://api.telegram.org/bottesttoken/sendMessage").mock(
        return_value=Response(429, json={"description": "Too Many Requests"})
    )
    msg_id = await publish_post(CONFIG, BASE_POST)
    assert msg_id is None
