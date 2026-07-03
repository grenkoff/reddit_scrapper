"""scrape_new_posts must enrich (fetch each post's page) only for posts new to the DB."""

import src.main as main
from src.config import Config

CONFIG = Config(telegram_bot_token="t", telegram_chat_id="t", database_url="postgresql://test", skip_nsfw=True)


def _post(rid, **extra):
    return {"reddit_id": rid, "is_nsfw": False, **extra}


async def test_scrape_enriches_and_inserts_only_new_posts(monkeypatch):
    enriched: list = []
    inserted: list = []
    existing = {"t3_old"}

    async def fake_fetch(_config):
        return [_post("t3_old"), _post("t3_new")]

    async def fake_exists(rid):
        return rid in existing

    async def fake_enrich(_client, post):
        enriched.append(post["reddit_id"])

    async def fake_insert(post):
        inserted.append(post["reddit_id"])

    async def fake_log(**_k):
        return None

    async def fake_sleep(_s):
        return None

    monkeypatch.setattr(main, "fetch_top_posts", fake_fetch)
    monkeypatch.setattr(main, "is_post_exists", fake_exists)
    monkeypatch.setattr(main, "enrich_post", fake_enrich)
    monkeypatch.setattr(main, "insert_post", fake_insert)
    monkeypatch.setattr(main, "log_scrape", fake_log)
    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)

    await main.scrape_new_posts(CONFIG)

    assert enriched == ["t3_new"]  # the already-stored post is not refetched
    assert inserted == ["t3_new"]


async def test_scrape_skips_nsfw_before_enriching(monkeypatch):
    enriched: list = []

    async def fake_fetch(_config):
        return [{"reddit_id": "t3_x", "is_nsfw": True}]

    async def fake_exists(_rid):
        return False

    async def fake_enrich(_client, post):
        enriched.append(post["reddit_id"])

    async def fake_insert(_post):
        return None

    async def fake_log(**_k):
        return None

    monkeypatch.setattr(main, "fetch_top_posts", fake_fetch)
    monkeypatch.setattr(main, "is_post_exists", fake_exists)
    monkeypatch.setattr(main, "enrich_post", fake_enrich)
    monkeypatch.setattr(main, "insert_post", fake_insert)
    monkeypatch.setattr(main, "log_scrape", fake_log)

    await main.scrape_new_posts(CONFIG)

    assert enriched == []  # NSFW filtered out, never fetched
