import json

import respx
from httpx import Response

from src.config import Config
from src.explainer.gemini import translate_comments
from src.publisher.telegram import _format_comment

CONFIG = Config(
    telegram_bot_token="t",
    telegram_chat_id="t",
    database_url="postgresql://test",
    gemini_api_key="key",
)
POST = {"reddit_id": "t3_abc", "title": "AK Shotgun variant from Pakistan"}
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent"


def _gemini_says(payload) -> Response:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return Response(200, json={"candidates": [{"content": {"parts": [{"text": text}]}}]})


# --- translate_comments ---


@respx.mock
async def test_translates_every_comment_in_one_request():
    route = respx.post(GEMINI_URL).mock(return_value=_gemini_says(["Первый", "Второй"]))
    comments = [{"body": "First"}, {"body": "Second"}]

    assert await translate_comments(CONFIG, POST, comments) == ["Первый", "Второй"]
    assert route.call_count == 1  # the whole batch costs one call, not one per comment


@respx.mock
async def test_media_only_comment_keeps_its_place():
    """A comment with no text is not sent for translation, but alignment must survive."""
    respx.post(GEMINI_URL).mock(return_value=_gemini_says(["Первый", "Третий"]))
    comments = [{"body": "First"}, {"body": ""}, {"body": "Third"}]

    assert await translate_comments(CONFIG, POST, comments) == ["Первый", None, "Третий"]


@respx.mock
async def test_mismatched_answer_is_discarded():
    """Shifted translations would caption comments with someone else's words — drop them all."""
    respx.post(GEMINI_URL).mock(return_value=_gemini_says(["Только один"]))
    comments = [{"body": "First"}, {"body": "Second"}]

    assert await translate_comments(CONFIG, POST, comments) == [None, None]


@respx.mock
async def test_unparseable_answer_is_discarded():
    respx.post(GEMINI_URL).mock(return_value=_gemini_says("not json at all"))

    assert await translate_comments(CONFIG, POST, [{"body": "First"}]) == [None]


@respx.mock
async def test_api_error_leaves_comments_untranslated():
    respx.post(GEMINI_URL).mock(return_value=Response(429, text="quota"))

    assert await translate_comments(CONFIG, POST, [{"body": "First"}]) == [None]


async def test_no_api_key_skips_translation():
    config = Config(telegram_bot_token="t", telegram_chat_id="t", database_url="postgresql://test")

    assert await translate_comments(config, POST, [{"body": "First"}]) == [None]


# --- _format_comment ---


def test_translation_is_hidden_behind_a_spoiler():
    text = _format_comment(
        {"author": "Evil_Knot", "body": "Pakistan is just Borderlands but irl."},
    )
    assert "tg-spoiler" not in text

    translated = _format_comment(
        {
            "author": "Evil_Knot",
            "body": "Pakistan is just Borderlands but irl.",
            "translation": "Пакистан — это Borderlands, только вживую.",
        }
    )
    assert "<tg-spoiler>Пакистан — это Borderlands, только вживую.</tg-spoiler>" in translated
    # The original stays readable above the spoiler.
    assert translated.index("Pakistan is just") < translated.index("<tg-spoiler>")


def test_translation_is_escaped_inside_the_spoiler():
    text = _format_comment({"author": "u", "body": "x", "translation": "<b>жирный</b> & прочее"})
    assert "<tg-spoiler>&lt;b&gt;жирный&lt;/b&gt; &amp; прочее</tg-spoiler>" in text
