import re

import httpx
import respx
from httpx import Response

from src.config import Config
from src.publisher.telegram import (
    MAX_CAPTION_LEN,
    MAX_MESSAGE_LEN,
    _build_footer,
    _build_media_texts,
    _chunk_text_evenly,
    _html_to_plain,
    _md_to_telegram_html,
    _publish_link,
    _publish_text_messages,
    _send_message,
    _take_raw_chunk,
    publish_post,
)


def _tags_balanced(s: str) -> bool:
    """No HTML tag is split across the message boundary."""
    opens = len(re.findall(r"<(?:b|i|s|code|a)(?:\s|>)", s))
    closes = len(re.findall(r"</(?:b|i|s|code|a)>", s))
    dangling = re.search(r"<[^>]*$", s) is not None
    return opens == closes and not dangling


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


# --- _take_raw_chunk (raw splitting keeps HTML valid) ---


def test_take_raw_chunk_whole_fits():
    html, rest = _take_raw_chunk("hello world", 1000)
    assert html == "hello world"
    assert rest == ""


def test_take_raw_chunk_respects_budget():
    html, rest = _take_raw_chunk("word " * 500, 100)
    assert len(html) <= 100
    assert rest  # there is leftover


def test_take_raw_chunk_never_splits_a_link_tag():
    # A markdown link straddling the budget must not leave a dangling <a ...> in the chunk.
    raw = "x" * 90 + " [click here](https://example.com/some/long/path) " + "tail " * 50
    html, rest = _take_raw_chunk(raw, 100)
    assert _tags_balanced(html)
    assert "<a" not in html or "</a>" in html


def test_take_raw_chunk_hard_cuts_oversized_token():
    # A single token longer than the budget still makes progress (no infinite loop).
    html, rest = _take_raw_chunk("a" * 500, 100)
    assert html
    assert len(rest) < 500


# --- _build_media_texts: HTML stays valid across the split ---


def test_media_caption_and_overflow_keep_tags_balanced():
    body = "x" * 930 + " [label](https://example.com/" + "a" * 200 + ") " + "tail " * 400
    post = {**BASE_POST, "post_type": "image", "content_url": "https://i.redd.it/a.jpg", "selftext": body}
    caption, overflow = _build_media_texts(post, CONFIG)
    assert len(caption) <= MAX_CAPTION_LEN
    assert _tags_balanced(caption)
    for msg in overflow:
        assert len(msg) <= MAX_MESSAGE_LEN
        assert _tags_balanced(msg)


def test_media_caption_link_pushed_whole_into_overflow_not_broken():
    body = "x" * 990 + " [label](https://example.com/link) " + "tail " * 400
    post = {**BASE_POST, "post_type": "image", "content_url": "https://i.redd.it/a.jpg", "selftext": body}
    caption, overflow = _build_media_texts(post, CONFIG)
    joined = caption + " ".join(overflow)
    # The link survives intact somewhere rather than being cut in half.
    assert '<a href="https://example.com/link">label</a>' in joined


def test_chunk_text_evenly_keeps_tags_balanced_when_link_straddles():
    body = "y" * 3000 + " [label](https://example.com/x) " + "z" * 3000
    chunks = _chunk_text_evenly(body, "footer")
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= MAX_MESSAGE_LEN
        assert _tags_balanced(chunk)


# --- _html_to_plain / plain-text fallback (Fix B) ---


def test_html_to_plain_strips_tags_and_unescapes():
    assert _html_to_plain('<b>Hi</b> <a href="u">x</a> a &amp; b &lt;c&gt;') == "Hi x a & b <c>"


@respx.mock
async def test_send_message_retries_as_plain_text_on_parse_error():
    responses = [
        Response(400, json={"description": "Bad Request: can't parse entities: unclosed tag"}),
        Response(200, json={"result": {"message_id": 77}}),
    ]
    sent = []

    def handler(request):
        sent.append(request.content.decode())
        return responses[len(sent) - 1]

    respx.post("https://api.telegram.org/bottesttoken/sendMessage").mock(side_effect=handler)
    async with httpx.AsyncClient() as client:
        msg_id = await _send_message(client, CONFIG, "<b>broken")
    assert msg_id == 77
    assert len(sent) == 2
    # Retry dropped parse_mode and stripped the tag.
    assert "parse_mode" not in sent[1]


@respx.mock
async def test_media_post_not_dropped_on_caption_parse_error():
    # sendPhoto rejects the HTML caption once, then the plain-text retry succeeds — the post
    # must reach Telegram instead of being silently dropped.
    responses = [
        Response(400, json={"description": "Bad Request: can't parse entities: unclosed tag"}),
        Response(200, json={"result": {"message_id": 55}}),
    ]
    calls = {"n": 0}

    def handler(request):
        r = responses[calls["n"]]
        calls["n"] += 1
        return r

    respx.post("https://api.telegram.org/bottesttoken/sendPhoto").mock(side_effect=handler)
    post = {**BASE_POST, "post_type": "link", "content_url": None, "preview_url": "https://img/x.jpg"}
    msg_id = await publish_post(CONFIG, post)
    assert msg_id == 55
    assert calls["n"] == 2


# --- _publish_link (downloaded preview) ---


@respx.mock
async def test_publish_link_sends_downloaded_photo_as_bytes(tmp_path):
    photo = tmp_path / "preview.jpg"
    photo.write_bytes(b"\xff\xd8\xff imagebytes")
    photo_route = respx.post("https://api.telegram.org/bottesttoken/sendPhoto").mock(
        return_value=Response(200, json={"result": {"message_id": 71}})
    )
    msg_route = respx.post("https://api.telegram.org/bottesttoken/sendMessage").mock(
        return_value=Response(200, json={"result": {"message_id": 99}})
    )
    post = {**BASE_POST, "post_type": "link", "preview_url": "https://external-preview.redd.it/x.jpg"}

    async with httpx.AsyncClient() as client:
        msg_id = await _publish_link(client, CONFIG, post, "caption", photo_path=photo)

    assert msg_id == 71
    # Bytes are sent as multipart/form-data (files=), not a URL form field.
    assert "multipart/form-data" in photo_route.calls.last.request.headers.get("content-type", "")
    assert not msg_route.called  # no text fallback when the photo succeeds


@respx.mock
async def test_publish_link_falls_back_to_url_then_text(tmp_path):
    photo = tmp_path / "preview.jpg"
    photo.write_bytes(b"\xff\xd8\xff imagebytes")
    # Both the byte send and the URL send fail → degrade to plain text.
    respx.post("https://api.telegram.org/bottesttoken/sendPhoto").mock(return_value=Response(400))
    msg_route = respx.post("https://api.telegram.org/bottesttoken/sendMessage").mock(
        return_value=Response(200, json={"result": {"message_id": 42}})
    )
    post = {**BASE_POST, "post_type": "link", "preview_url": "https://external-preview.redd.it/x.jpg"}

    async with httpx.AsyncClient() as client:
        msg_id = await _publish_link(client, CONFIG, post, "caption", photo_path=photo)

    assert msg_id == 42
    assert msg_route.called  # fell all the way back to text


# --- _publish_text_messages ---


@respx.mock
async def test_publish_text_single_message():
    respx.post("https://api.telegram.org/bottesttoken/sendMessage").mock(
        return_value=Response(200, json={"result": {"message_id": 1}})
    )
    post = {**BASE_POST, "selftext": "Short text."}

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

    async with httpx.AsyncClient() as client:
        await _publish_text_messages(client, CONFIG, post)
    assert len(call_bodies) >= 2
    assert "..." in call_bodies[0]
