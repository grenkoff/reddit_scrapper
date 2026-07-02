import respx
from httpx import Response

from src.config import Config
from src.publisher.telegram import _build_media_texts, publish_failed_notice, publish_post


async def _no_sleep(*_args, **_kwargs):
    return None


CONFIG = Config(
    telegram_bot_token="testtoken",
    telegram_chat_id="-100123456",
    database_url="postgresql://test",
    pause_between_posts=0,
)

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
    caption, _ = _build_media_texts(BASE_POST, CONFIG)
    assert "Test post title" in caption


def test_caption_contains_footer():
    caption, _ = _build_media_texts(BASE_POST, CONFIG)
    assert "r/programming" in caption


def test_caption_contains_subreddit():
    caption, _ = _build_media_texts(BASE_POST, CONFIG)
    assert "r/programming" in caption


def test_caption_contains_url():
    caption, _ = _build_media_texts(BASE_POST, CONFIG)
    assert BASE_POST["url"] in caption


def test_caption_overflow_no_duplication():
    post = {**BASE_POST, "selftext": "word " * 300}
    caption, overflow = _build_media_texts(post, CONFIG)
    assert len(caption) <= 1024
    assert len(overflow) > 0
    # Caption text should not repeat in overflow
    caption_text = caption.split("\n\n", 1)[1] if "\n\n" in caption else ""
    for msg in overflow:
        assert caption_text not in msg


def test_caption_max_length():
    post = {**BASE_POST, "title": "T" * 500, "selftext": "S" * 600}
    caption, _ = _build_media_texts(post, CONFIG)
    assert len(caption) <= 1024


@respx.mock
async def test_publish_text_post_returns_message_id():
    respx.post("https://api.telegram.org/bottesttoken/sendMessage").mock(
        return_value=Response(200, json={"result": {"message_id": 42}})
    )
    msg_id = await publish_post(CONFIG, BASE_POST)
    assert msg_id == 42


@respx.mock
async def test_publish_text_post_on_api_failure(monkeypatch):
    # Persistent 429 → give up after retries and return None (retries mustn't hang on real sleeps).
    monkeypatch.setattr("src.publisher.telegram.asyncio.sleep", _no_sleep)
    route = respx.post("https://api.telegram.org/bottesttoken/sendMessage").mock(
        return_value=Response(429, json={"description": "Too Many Requests"})
    )
    msg_id = await publish_post(CONFIG, BASE_POST)
    assert msg_id is None
    assert route.call_count > 1  # initial send + retries


@respx.mock
async def test_publish_text_post_retries_then_succeeds_on_429(monkeypatch):
    monkeypatch.setattr("src.publisher.telegram.asyncio.sleep", _no_sleep)
    responses = [
        Response(429, json={"parameters": {"retry_after": 1}}),
        Response(200, json={"result": {"message_id": 77}}),
    ]
    respx.post("https://api.telegram.org/bottesttoken/sendMessage").mock(side_effect=responses)
    msg_id = await publish_post(CONFIG, BASE_POST)
    assert msg_id == 77


@respx.mock
async def test_publish_failed_notice_sends_post_link():
    route = respx.post("https://api.telegram.org/bottesttoken/sendMessage").mock(
        return_value=Response(200, json={"result": {"message_id": 5}})
    )
    link_post = {**BASE_POST, "post_type": "link", "content_url": "https://example.com/article"}
    msg_id = await publish_failed_notice(CONFIG, link_post)
    assert msg_id == 5
    sent = route.calls.last.request.content.decode()
    assert "reddit.com%2Fr%2Fprogramming" in sent or "reddit.com/r/programming" in sent
    assert "example.com" in sent
