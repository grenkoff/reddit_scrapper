import pytest

from src.db import get_unpublished_posts, init_db, insert_post, is_post_exists, mark_as_published

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
}


@pytest.fixture(autouse=True)
async def setup_db(tmp_path, monkeypatch):
    monkeypatch.setattr("src.db.DB_PATH", tmp_path / "test.db")
    await init_db()


async def test_insert_and_exists():
    await insert_post(SAMPLE_POST)
    assert await is_post_exists("t3_abc123") is True


async def test_not_exists():
    assert await is_post_exists("t3_nonexistent") is False


async def test_deduplication():
    await insert_post(SAMPLE_POST)
    await insert_post(SAMPLE_POST)  # второй insert должен игнорироваться
    posts = await get_unpublished_posts()
    assert len(posts) == 1


async def test_mark_as_published():
    await insert_post(SAMPLE_POST)
    await mark_as_published("t3_abc123", tg_message_id=42)
    posts = await get_unpublished_posts()
    assert len(posts) == 0
