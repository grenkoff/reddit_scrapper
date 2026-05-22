from unittest.mock import AsyncMock, patch

from httpx import ASGITransport, AsyncClient

from src.config import Config
from src.webapp.server import create_app

CONFIG = Config(
    telegram_bot_token="test",
    telegram_chat_id="test",
    database_url="postgresql://test",
    gemini_api_key="testkey",
    reddit_proxy_secret="mysecret",
)


async def _client(config=CONFIG):
    app = create_app(config)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# --- /health ---


async def test_health_returns_ok():
    async with await _client() as c:
        resp = await c.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


# --- /admin/reset ---


async def test_admin_reset_wrong_secret():
    async with await _client() as c:
        resp = await c.post("/admin/reset?reddit_id=t3_abc&secret=wrong")
    assert resp.status_code == 401


async def test_admin_reset_correct_secret():
    with patch("src.webapp.server.mark_as_unpublished", new_callable=AsyncMock) as mock_reset:
        async with await _client() as c:
            resp = await c.post("/admin/reset?reddit_id=t3_abc&secret=mysecret")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    mock_reset.assert_awaited_once_with("t3_abc")


# --- /api/explain ---


async def test_explain_returns_cached():
    with patch("src.webapp.server.get_explanation", new_callable=AsyncMock, return_value="Cached text."):
        async with await _client() as c:
            resp = await c.get("/api/explain?reddit_id=t3_abc")
    assert resp.status_code == 200
    assert resp.json()["cached"] is True
    assert "Cached text." in resp.json()["explanation"]


async def test_explain_returns_error_when_not_cached():
    with patch("src.webapp.server.get_explanation", new_callable=AsyncMock, return_value=None):
        async with await _client() as c:
            resp = await c.get("/api/explain?reddit_id=t3_abc")
    assert "error" in resp.json()


# --- /api/explain/stream ---


async def test_explain_stream_serves_cached_explanation():
    long_cached = "A" * 60 + "."
    with (
        patch("src.webapp.server.get_explanation", new_callable=AsyncMock, return_value=long_cached),
        patch("src.webapp.server.get_translated_image", new_callable=AsyncMock, return_value=None),
    ):
        async with await _client() as c:
            resp = await c.get("/api/explain/stream?reddit_id=t3_abc")
    assert resp.status_code == 200
    assert long_cached in resp.text


async def test_explain_stream_ignores_short_cache():
    with (
        patch("src.webapp.server.get_explanation", new_callable=AsyncMock, return_value="short"),
        patch("src.webapp.server.get_post", new_callable=AsyncMock, return_value=None),
    ):
        async with await _client() as c:
            resp = await c.get("/api/explain/stream?reddit_id=t3_abc")
    assert "Пост не найден" in resp.text


async def test_explain_stream_post_not_found():
    with (
        patch("src.webapp.server.get_explanation", new_callable=AsyncMock, return_value=None),
        patch("src.webapp.server.get_post", new_callable=AsyncMock, return_value=None),
    ):
        async with await _client() as c:
            resp = await c.get("/api/explain/stream?reddit_id=t3_missing")
    assert "Пост не найден" in resp.text


async def test_explain_stream_no_gemini_key():
    config_no_ai = Config(
        telegram_bot_token="test",
        telegram_chat_id="test",
        database_url="postgresql://test",
        gemini_api_key=None,
        reddit_proxy_secret="mysecret",
    )
    with patch("src.webapp.server.get_explanation", new_callable=AsyncMock, return_value=None):
        async with await _client(config_no_ai) as c:
            resp = await c.get("/api/explain/stream?reddit_id=t3_abc")
    assert "AI не настроен" in resp.text


async def test_explain_stream_caches_only_complete_response():
    fake_post = {"reddit_id": "t3_abc", "title": "Test", "post_type": "image"}

    async def fake_stream(config, post, skip_image_text=False):
        yield "Complete answer."

    with (
        patch("src.webapp.server.get_explanation", new_callable=AsyncMock, return_value=None),
        patch("src.webapp.server.get_post", new_callable=AsyncMock, return_value=fake_post),
        patch("src.webapp.server.get_translated_image", new_callable=AsyncMock, return_value=None),
        patch("src.webapp.server.detect_image_text", new_callable=AsyncMock, return_value=[]),
        patch("src.webapp.server.stream_explanation", side_effect=fake_stream),
        patch("src.webapp.server.save_explanation", new_callable=AsyncMock) as mock_save,
    ):
        async with await _client() as c:
            await c.get("/api/explain/stream?reddit_id=t3_abc")
    mock_save.assert_awaited_once()


async def test_explain_stream_does_not_cache_truncated_response():
    fake_post = {"reddit_id": "t3_abc", "title": "Test", "post_type": "image"}

    async def fake_stream(config, post, skip_image_text=False):
        yield "Truncated mid-sentenc"  # no ending punctuation

    with (
        patch("src.webapp.server.get_explanation", new_callable=AsyncMock, return_value=None),
        patch("src.webapp.server.get_post", new_callable=AsyncMock, return_value=fake_post),
        patch("src.webapp.server.get_translated_image", new_callable=AsyncMock, return_value=None),
        patch("src.webapp.server.detect_image_text", new_callable=AsyncMock, return_value=[]),
        patch("src.webapp.server.stream_explanation", side_effect=fake_stream),
        patch("src.webapp.server.save_explanation", new_callable=AsyncMock) as mock_save,
    ):
        async with await _client() as c:
            await c.get("/api/explain/stream?reddit_id=t3_abc")
    mock_save.assert_not_awaited()
