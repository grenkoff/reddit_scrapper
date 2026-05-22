import respx
from httpx import Response

from src.config import Config
from src.publisher.telegram import (
    _build_footer,
    _chunk_text_evenly,
    _md_to_telegram_html,
    _publish_text_messages,
)

CONFIG = Config(
    telegram_bot_token="testtoken",
    telegram_chat_id="-100123456",
    database_url="postgresql://test",
    pause_between_posts=0,
)
CONFIG_WITH_AI = Config(
    telegram_bot_token="testtoken",
    telegram_chat_id="-100123456",
    database_url="postgresql://test",
    gemini_api_key="key",
    telegram_channel_link="https://t.me/testchan",
    pause_between_posts=0,
    bot_username="mybot",
)

BASE_POST = {
    "reddit_id": "t3_abc",
    "subreddit": "funny",
    "title": "Title",
    "author": "user",
    "url": "https://reddit.com/r/funny/comments/abc",
    "content_url": None,
    "selftext": None,
    "score": 100,
    "num_comments": 10,
    "post_type": "text",
    "is_nsfw": False,
    "media_urls": None,
}


# --- _md_to_telegram_html ---


def test_md_bold_double_star():
    assert "<b>hello</b>" in _md_to_telegram_html("**hello**")


def test_md_bold_underscore():
    assert "<b>hello</b>" in _md_to_telegram_html("__hello__")


def test_md_italic_star():
    assert "<i>hi</i>" in _md_to_telegram_html("*hi*")


def test_md_italic_underscore():
    assert "<i>hi</i>" in _md_to_telegram_html("_hi_")


def test_md_strikethrough():
    assert "<s>old</s>" in _md_to_telegram_html("~~old~~")


def test_md_code():
    assert "<code>x</code>" in _md_to_telegram_html("`x`")


def test_md_link():
    result = _md_to_telegram_html("[Google](https://google.com)")
    assert '<a href="https://google.com">Google</a>' in result


def test_md_heading():
    result = _md_to_telegram_html("# Title")
    assert "<b>Title</b>" in result


def test_md_html_escape():
    result = _md_to_telegram_html("a < b & c > d")
    assert "&lt;" in result
    assert "&amp;" in result
    assert "&gt;" in result


def test_md_plain_text_unchanged():
    result = _md_to_telegram_html("just plain text")
    assert "just plain text" in result


# --- _build_footer ---


def test_footer_contains_subreddit_link():
    footer = _build_footer(BASE_POST, CONFIG)
    assert "r/funny" in footer


def test_footer_no_ai_link_without_key():
    footer = _build_footer(BASE_POST, CONFIG)
    assert "AI-explanation" not in footer


def test_footer_has_ai_link_with_key_and_username():
    footer = _build_footer(BASE_POST, CONFIG_WITH_AI)
    assert "AI-explanation" in footer
    assert "t3_abc" in footer


def test_footer_has_channel_link():
    footer = _build_footer(BASE_POST, CONFIG_WITH_AI)
    assert "https://t.me/testchan" in footer


def test_footer_link_post_shows_domain():
    post = {**BASE_POST, "post_type": "link", "content_url": "https://example.com/article"}
    footer = _build_footer(post, CONFIG)
    assert "example.com" in footer


def test_footer_link_post_skips_reddit_domain():
    post = {**BASE_POST, "post_type": "link", "content_url": "https://reddit.com/gallery/abc"}
    footer = _build_footer(post, CONFIG)
    assert "reddit.com" not in footer.split("r/funny")[1]


# --- _chunk_text_evenly ---


def test_chunk_evenly_single_chunk():
    chunks = _chunk_text_evenly("short", "footer")
    assert len(chunks) == 1
    assert "short" in chunks[0]
    assert "footer" in chunks[0]


def test_chunk_evenly_multiple_chunks():
    long_body = "word " * 900
    chunks = _chunk_text_evenly(long_body, "footer")
    assert len(chunks) > 1


def test_chunk_evenly_last_chunk_has_footer():
    chunks = _chunk_text_evenly("word " * 900, "my_footer")
    assert "my_footer" in chunks[-1]


def test_chunk_evenly_middle_chunks_have_prefix():
    chunks = _chunk_text_evenly("word " * 900, "footer")
    for chunk in chunks[:-1]:
        assert "\U0001f4ac" in chunk or "..." in chunk


# --- _publish_text_messages ---


@respx.mock
async def test_publish_text_single_message():
    respx.post("https://api.telegram.org/bottesttoken/sendMessage").mock(
        return_value=Response(200, json={"result": {"message_id": 1}})
    )
    post = {**BASE_POST, "selftext": "Short text."}
    import httpx

    async with httpx.AsyncClient() as client:
        msg_id = await _publish_text_messages(client, CONFIG, post)
    assert msg_id == 1


@respx.mock
async def test_publish_text_split_adds_continuation_marker():
    call_bodies = []

    def capture(request):
        call_bodies.append(request.content.decode())
        return Response(200, json={"result": {"message_id": len(call_bodies)}})

    respx.post("https://api.telegram.org/bottesttoken/sendMessage").mock(side_effect=capture)
    # 900 words × 5 chars = 4500 chars → exceeds MAX_MESSAGE_LEN (4096) → forces split
    post = {**BASE_POST, "selftext": "word " * 900}
    import httpx

    async with httpx.AsyncClient() as client:
        await _publish_text_messages(client, CONFIG, post)
    assert len(call_bodies) >= 2
    assert "\U0001f4ac" in call_bodies[1] or "..." in call_bodies[1]


@respx.mock
async def test_publish_text_first_message_ends_with_ellipsis_when_split():
    call_bodies = []

    def capture(request):
        call_bodies.append(request.content.decode())
        return Response(200, json={"result": {"message_id": len(call_bodies)}})

    respx.post("https://api.telegram.org/bottesttoken/sendMessage").mock(side_effect=capture)
    post = {**BASE_POST, "selftext": "word " * 900}
    import httpx

    async with httpx.AsyncClient() as client:
        await _publish_text_messages(client, CONFIG, post)
    assert len(call_bodies) >= 2
    assert "..." in call_bodies[0]
