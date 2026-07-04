"""Tests for publish_one's failure handling: no post must vanish without a link reaching Telegram."""

from pathlib import Path

import src.main as main
from src.config import Config
from src.publisher.poller import UpdatePoller

PREVIEW_LINK_POST = {
    "reddit_id": "t3_lnk",
    "subreddit": "news",
    "title": "Headline",
    "url": "https://reddit.com/r/news/comments/lnk/headline/",
    "content_url": "https://bbc.co.uk/article",
    "post_type": "link",
    "preview_url": "https://external-preview.redd.it/x.jpg",
}

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


async def test_publish_one_video_download_failure_drops_without_notice(monkeypatch):
    notices: list = []
    published: dict = {}

    async def fake_get(limit=None, max_age_hours=24):
        return [{**LINK_POST, "post_type": "video", "video_url": "https://v.redd.it/x/DASH_720.mp4"}]

    async def fake_fresh_hls(_config, _rid):
        return None

    async def fake_dl_video(_url, hls_url=None):
        return None  # undownloadable video

    async def fake_notice(_config, post):
        notices.append(post["reddit_id"])
        return 5

    async def fake_mark(reddit_id, msg_id):
        published[reddit_id] = msg_id

    monkeypatch.setattr(main, "get_unpublished_posts", fake_get)
    monkeypatch.setattr(main, "fetch_fresh_hls_url", fake_fresh_hls)
    monkeypatch.setattr(main, "download_video_direct", fake_dl_video)
    monkeypatch.setattr(main, "publish_failed_notice", fake_notice)
    monkeypatch.setattr(main, "mark_as_published", fake_mark)

    result = await main.publish_one(CONFIG, UpdatePoller(CONFIG))

    assert result is False
    assert notices == []  # no link-only notice for an undownloadable video
    assert published == {"t3_lnk": 0}  # still marked handled so it isn't retried forever


async def test_publish_one_downloads_link_preview_and_passes_photo_path(monkeypatch):
    captured: dict = {}
    cleaned: list = []
    fake_photo = Path("tmp/fake_preview.jpg")

    async def fake_get(limit=None, max_age_hours=24):
        return [dict(PREVIEW_LINK_POST)]

    async def fake_download_image(url):
        assert url == PREVIEW_LINK_POST["preview_url"]
        return fake_photo

    async def fake_publish_post(config, post, media_path=None, media_paths=None, photo_path=None):
        captured["photo_path"] = photo_path
        return 10

    async def fake_mark(_reddit_id, _msg_id):
        return None

    async def fake_comments(*_a, **_k):
        return None

    monkeypatch.setattr(main, "get_unpublished_posts", fake_get)
    monkeypatch.setattr(main, "download_image", fake_download_image)
    monkeypatch.setattr(main, "publish_post", fake_publish_post)
    monkeypatch.setattr(main, "mark_as_published", fake_mark)
    monkeypatch.setattr(main, "_publish_comments_delayed", fake_comments)
    monkeypatch.setattr(main, "cleanup", lambda p: cleaned.append(p))

    result = await main.publish_one(CONFIG, UpdatePoller(CONFIG))

    assert result is True
    assert captured["photo_path"] == fake_photo  # downloaded preview reached publish_post as bytes
    assert fake_photo in cleaned  # temp preview file cleaned up


async def test_publish_one_link_preview_download_failure_is_not_dropped(monkeypatch):
    captured: dict = {}
    notices: list = []

    async def fake_get(limit=None, max_age_hours=24):
        return [dict(PREVIEW_LINK_POST)]

    async def fake_download_image(_url):
        return None  # preview download fails

    async def fake_publish_post(config, post, media_path=None, media_paths=None, photo_path=None):
        captured["photo_path"] = photo_path
        return 10

    async def fake_notice(_config, post):
        notices.append(post["reddit_id"])
        return 5

    async def fake_mark(_reddit_id, _msg_id):
        return None

    async def fake_comments(*_a, **_k):
        return None

    monkeypatch.setattr(main, "get_unpublished_posts", fake_get)
    monkeypatch.setattr(main, "download_image", fake_download_image)
    monkeypatch.setattr(main, "publish_post", fake_publish_post)
    monkeypatch.setattr(main, "publish_failed_notice", fake_notice)
    monkeypatch.setattr(main, "mark_as_published", fake_mark)
    monkeypatch.setattr(main, "_publish_comments_delayed", fake_comments)
    monkeypatch.setattr(main, "cleanup", lambda _p: None)

    result = await main.publish_one(CONFIG, UpdatePoller(CONFIG))

    assert result is True  # still published (degrades to URL/text inside _publish_link)
    assert captured["photo_path"] is None  # publish_post called with no photo
    assert notices == []  # a failed preview download must NOT drop the post
