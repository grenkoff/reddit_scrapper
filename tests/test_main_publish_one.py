"""Tests for publish_one's failure handling: no post must vanish without a link reaching Telegram."""

import src.main as main
from src.config import Config
from src.publisher.poller import UpdatePoller

CONFIG = Config(telegram_bot_token="testtoken", telegram_chat_id="-100", database_url="postgresql://test")

LINK_POST = {
    "reddit_id": "t3_lnk",
    "subreddit": "news",
    "title": "Headline",
    "url": "https://reddit.com/r/news/comments/lnk/headline/",
    "content_url": "https://example.com/article",
    "post_type": "text",
    "selftext": "body",
}


async def test_publish_one_sends_notice_when_publish_fails(monkeypatch):
    published: dict = {}
    notices: list = []

    async def fake_get(limit=None, max_age_hours=24):
        return [dict(LINK_POST)]

    async def fake_publish_post(*_a, **_k):
        return None  # publishing fails

    async def fake_notice(_config, post):
        notices.append(post["reddit_id"])
        return 999

    async def fake_mark(reddit_id, msg_id):
        published[reddit_id] = msg_id

    monkeypatch.setattr(main, "get_unpublished_posts", fake_get)
    monkeypatch.setattr(main, "publish_post", fake_publish_post)
    monkeypatch.setattr(main, "publish_failed_notice", fake_notice)
    monkeypatch.setattr(main, "mark_as_published", fake_mark)

    result = await main.publish_one(CONFIG, UpdatePoller(CONFIG))

    assert result is False  # not published normally
    assert notices == ["t3_lnk"]  # link-only notice was sent
    assert published == {"t3_lnk": 999}  # marked handled with the notice's message id


async def test_publish_one_sends_notice_when_media_download_fails(monkeypatch):
    notices: list = []

    async def fake_get(limit=None, max_age_hours=24):
        return [{**LINK_POST, "post_type": "image", "content_url": "https://i.redd.it/x.jpg"}]

    async def fake_download_image(_url):
        return None  # download fails

    async def fake_notice(_config, post):
        notices.append(post["reddit_id"])
        return 5

    async def fake_mark(_reddit_id, _msg_id):
        return None

    monkeypatch.setattr(main, "get_unpublished_posts", fake_get)
    monkeypatch.setattr(main, "download_image", fake_download_image)
    monkeypatch.setattr(main, "publish_failed_notice", fake_notice)
    monkeypatch.setattr(main, "mark_as_published", fake_mark)

    result = await main.publish_one(CONFIG, UpdatePoller(CONFIG))

    assert result is False
    assert notices == ["t3_lnk"]  # image posts that fail to download still send their link
