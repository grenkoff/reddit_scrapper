import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import src.db as db_module
from src.db import (
    get_explanation,
    get_post,
    get_unpublished_posts,
    log_scrape,
    mark_as_unpublished,
    save_explanation,
)


class _AsyncCtx:
    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *_):
        pass


def _make_pool(fetchrow_return=None, fetch_return=None, execute_return="UPDATE 1"):
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=fetchrow_return)
    conn.fetch = AsyncMock(return_value=fetch_return or [])
    conn.execute = AsyncMock(return_value=execute_return)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncCtx(conn))
    return pool, conn


# --- get_unpublished_posts ---


async def test_get_unpublished_posts_empty():
    pool, conn = _make_pool(fetch_return=[])
    with patch.object(db_module, "_pool", pool):
        posts = await get_unpublished_posts()
    assert posts == []


async def test_get_unpublished_posts_with_limit():
    pool, conn = _make_pool(fetch_return=[])
    with patch.object(db_module, "_pool", pool):
        await get_unpublished_posts(limit=5)
    sql = conn.fetch.call_args.args[0]
    assert "LIMIT 5" in sql


async def test_get_unpublished_posts_24h_filter():
    pool, conn = _make_pool(fetch_return=[])
    with patch.object(db_module, "_pool", pool):
        await get_unpublished_posts()
    sql = conn.fetch.call_args.args[0]
    assert "24 hours" in sql


async def test_get_unpublished_posts_deserializes_media_urls():
    row = MagicMock()
    row.__getitem__ = lambda self, k: json.dumps(["url1", "url2"]) if k == "media_urls" else None
    row.keys = lambda: ["media_urls"]

    def dict_row():
        return {"media_urls": json.dumps(["url1", "url2"])}

    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[{"media_urls": json.dumps(["url1", "url2"])}])
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncCtx(conn))
    with patch.object(db_module, "_pool", pool):
        posts = await get_unpublished_posts()
    assert posts[0]["media_urls"] == ["url1", "url2"]


async def test_get_unpublished_posts_null_media_urls_stays_none():
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[{"media_urls": None}])
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncCtx(conn))
    with patch.object(db_module, "_pool", pool):
        posts = await get_unpublished_posts()
    assert posts[0]["media_urls"] is None


# --- mark_as_unpublished ---


async def test_mark_as_unpublished_clears_fields():
    pool, conn = _make_pool()
    with patch.object(db_module, "_pool", pool):
        await mark_as_unpublished("t3_abc123")
    sql, reddit_id = conn.execute.call_args.args
    assert "published_to_tg = FALSE" in sql
    assert "published_at = NULL" in sql
    assert "tg_message_id = NULL" in sql
    assert reddit_id == "t3_abc123"


# --- get_post ---


async def test_get_post_found():
    pool, conn = _make_pool(fetchrow_return={"reddit_id": "t3_abc", "media_urls": None})
    with patch.object(db_module, "_pool", pool):
        post = await get_post("t3_abc")
    assert post["reddit_id"] == "t3_abc"


async def test_get_post_not_found():
    pool, conn = _make_pool(fetchrow_return=None)
    with patch.object(db_module, "_pool", pool):
        post = await get_post("t3_missing")
    assert post is None


async def test_get_post_deserializes_media_urls():
    pool, conn = _make_pool(fetchrow_return={"reddit_id": "t3_x", "media_urls": '["a","b"]'})
    with patch.object(db_module, "_pool", pool):
        post = await get_post("t3_x")
    assert post["media_urls"] == ["a", "b"]


# --- get_explanation / save_explanation ---


async def test_get_explanation_returns_text():
    pool, conn = _make_pool(fetchrow_return={"ai_explanation": "Some explanation."})
    with patch.object(db_module, "_pool", pool):
        result = await get_explanation("t3_abc")
    assert result == "Some explanation."


async def test_get_explanation_returns_none_when_missing():
    pool, conn = _make_pool(fetchrow_return=None)
    with patch.object(db_module, "_pool", pool):
        result = await get_explanation("t3_abc")
    assert result is None


async def test_save_explanation_passes_correct_args():
    pool, conn = _make_pool()
    with patch.object(db_module, "_pool", pool):
        await save_explanation("t3_abc", "My explanation.")
    _, explanation, reddit_id = conn.execute.call_args.args
    assert explanation == "My explanation."
    assert reddit_id == "t3_abc"


# --- log_scrape ---


async def test_log_scrape_inserts_row():
    pool, conn = _make_pool()
    now = datetime.now(UTC)
    with patch.object(db_module, "_pool", pool):
        await log_scrape(now, now, posts_found=10, posts_new=3, posts_published=1)
    sql = conn.execute.call_args.args[0]
    assert "INSERT INTO scrape_logs" in sql
