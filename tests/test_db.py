from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import src.db as db_module
from src.db import get_unpublished_posts, insert_post, is_post_exists, mark_as_published

SAMPLE_POST = {
    "reddit_id": "t3_abc123",
    "subreddit": "programming",
    "title": "Test post",
    "author": "test_user",
    "url": "https://reddit.com/r/programming/comments/abc123",
    "content_url": "https://i.redd.it/test.jpg",
    "selftext": None,
    "score": 1000,
    "num_comments": 50,
    "post_type": "image",
    "is_nsfw": False,
    "media_urls": None,
    "created_utc": "2026-01-01T00:00:00",
    "preview_url": None,
    "video_url": None,
    "hls_url": None,
}


def _make_pool(fetchrow_return=None, fetch_return=None):
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=fetchrow_return)
    conn.fetch = AsyncMock(return_value=fetch_return or [])
    conn.execute = AsyncMock()

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncCtx(conn))
    return pool, conn


class _AsyncCtx:
    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *_):
        pass


async def test_insert_and_exists():
    pool, conn = _make_pool(fetchrow_return={"1": 1})
    with patch.object(db_module, "_pool", pool):
        assert await is_post_exists("t3_abc123") is True
        conn.fetchrow.assert_awaited_once_with(
            "SELECT 1 FROM posts WHERE reddit_id = $1", "t3_abc123"
        )


async def test_not_exists():
    pool, conn = _make_pool(fetchrow_return=None)
    with patch.object(db_module, "_pool", pool):
        assert await is_post_exists("t3_nonexistent") is False


async def test_deduplication():
    pool, conn = _make_pool()
    with patch.object(db_module, "_pool", pool):
        await insert_post(SAMPLE_POST)
        await insert_post(SAMPLE_POST)
    # ON CONFLICT DO NOTHING — both calls go through but DB handles dedup
    assert conn.execute.await_count == 2


async def test_mark_as_published():
    pool, conn = _make_pool()
    with patch.object(db_module, "_pool", pool):
        await mark_as_published("t3_abc123", tg_message_id=42)
    call_args = conn.execute.call_args
    assert "t3_abc123" in call_args.args
    assert 42 in call_args.args
