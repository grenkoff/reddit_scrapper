"""scrape_new_posts must store only posts new to the DB, in a single Reddit request."""

import src.main as main
from src.config import Config

CONFIG = Config(telegram_bot_token="t", telegram_chat_id="t", database_url="postgresql://test", skip_nsfw=True)


def _post(rid, **extra):
    return {"reddit_id": rid, "is_nsfw": False, **extra}


async def test_scrape_inserts_only_new_posts(monkeypatch):
    inserted: list = []
    existing = {"t3_old"}

    async def fake_fetch(_config):
        return [_post("t3_old"), _post("t3_new")]

    async def fake_exists(rid):
        return rid in existing

    async def fake_insert(post):
        inserted.append(post["reddit_id"])

    async def fake_log(**_k):
        return None

    monkeypatch.setattr(main, "fetch_top_posts", fake_fetch)
    monkeypatch.setattr(main, "is_post_exists", fake_exists)
    monkeypatch.setattr(main, "insert_post", fake_insert)
    monkeypatch.setattr(main, "log_scrape", fake_log)

    await main.scrape_new_posts(CONFIG)

    assert inserted == ["t3_new"]  # the already-stored post is left alone


async def test_scrape_skips_nsfw(monkeypatch):
    inserted: list = []

    async def fake_fetch(_config):
        return [{"reddit_id": "t3_x", "is_nsfw": True}]

    async def fake_exists(_rid):
        return False

    async def fake_insert(post):
        inserted.append(post["reddit_id"])

    async def fake_log(**_k):
        return None

    monkeypatch.setattr(main, "fetch_top_posts", fake_fetch)
    monkeypatch.setattr(main, "is_post_exists", fake_exists)
    monkeypatch.setattr(main, "insert_post", fake_insert)
    monkeypatch.setattr(main, "log_scrape", fake_log)

    await main.scrape_new_posts(CONFIG)

    assert inserted == []


async def test_scrape_logs_counts(monkeypatch):
    logged: dict = {}

    async def fake_fetch(_config):
        return [_post("t3_a"), _post("t3_b")]

    async def fake_exists(rid):
        return rid == "t3_a"

    async def fake_insert(_post):
        return None

    async def fake_log(**kwargs):
        logged.update(kwargs)

    monkeypatch.setattr(main, "fetch_top_posts", fake_fetch)
    monkeypatch.setattr(main, "is_post_exists", fake_exists)
    monkeypatch.setattr(main, "insert_post", fake_insert)
    monkeypatch.setattr(main, "log_scrape", fake_log)

    await main.scrape_new_posts(CONFIG)

    assert (logged["posts_found"], logged["posts_new"], logged["error"]) == (2, 1, None)
