import json
from pathlib import Path

import respx
from httpx import Response

from src.config import Config
from src.scraper.reddit import fetch_fresh_hls_url, fetch_top_comments, fetch_top_posts

FIXTURE = json.loads((Path("tests/fixtures/reddit_top.json")).read_text())

CONFIG = Config(
    telegram_bot_token="test",
    telegram_chat_id="test",
    database_url="postgresql://test",
    reddit_user_agent="test/0.1",
)
CONFIG_WITH_PROXY = Config(
    telegram_bot_token="test",
    telegram_chat_id="test",
    database_url="postgresql://test",
    reddit_user_agent="test/0.1",
    reddit_proxy_url="https://proxy.example.com",
    reddit_proxy_secret="mysecret",
)

SAMPLE_POST = {
    "reddit_id": "t3_abc123",
    "subreddit": "funny",
    "url": "https://reddit.com/r/funny/comments/abc123",
}


# --- fetch_top_posts ---


@respx.mock
async def test_fetch_top_posts_skips_removed():
    fixture = json.loads(json.dumps(FIXTURE))
    fixture["data"]["children"][0]["data"]["removed_by_category"] = "spam"
    respx.get("https://www.reddit.com/.json").mock(return_value=Response(200, json=fixture))
    posts = await fetch_top_posts(CONFIG)
    assert len(posts) == 5  # one removed


@respx.mock
async def test_fetch_top_posts_skips_deleted_author():
    fixture = json.loads(json.dumps(FIXTURE))
    fixture["data"]["children"][0]["data"]["author"] = "[deleted]"
    respx.get("https://www.reddit.com/.json").mock(return_value=Response(200, json=fixture))
    posts = await fetch_top_posts(CONFIG)
    assert len(posts) == 5


@respx.mock
async def test_fetch_top_posts_uses_proxy_url_and_secret():
    respx.get("https://proxy.example.com/.json").mock(return_value=Response(200, json=FIXTURE))
    posts = await fetch_top_posts(CONFIG_WITH_PROXY)
    assert len(posts) == 6
    request = respx.calls.last.request
    assert request.headers["X-Proxy-Secret"] == "mysecret"


# --- fetch_top_comments ---

COMMENTS_FIXTURE = [
    {"data": {"children": []}},
    {
        "data": {
            "children": [
                {
                    "kind": "t1",
                    "data": {
                        "author": "user1",
                        "body": "Great post!",
                        "score": 500,
                        "stickied": False,
                    },
                },
                {
                    "kind": "t1",
                    "data": {
                        "author": "[deleted]",
                        "body": "[removed]",
                        "score": 10,
                        "stickied": False,
                    },
                },
                {
                    "kind": "t1",
                    "data": {
                        "author": "mod",
                        "body": "Stickied comment",
                        "score": 1,
                        "stickied": True,
                    },
                },
            ]
        }
    },
]


@respx.mock
async def test_fetch_top_comments_filters_deleted():
    respx.get("https://www.reddit.com/r/funny/comments/abc123.json").mock(
        return_value=Response(200, json=COMMENTS_FIXTURE)
    )
    comments = await fetch_top_comments(CONFIG, SAMPLE_POST)
    authors = [c["author"] for c in comments]
    assert "[deleted]" not in authors


@respx.mock
async def test_fetch_top_comments_filters_stickied():
    respx.get("https://www.reddit.com/r/funny/comments/abc123.json").mock(
        return_value=Response(200, json=COMMENTS_FIXTURE)
    )
    comments = await fetch_top_comments(CONFIG, SAMPLE_POST)
    bodies = [c["body"] for c in comments]
    assert "Stickied comment" not in bodies


@respx.mock
async def test_fetch_top_comments_returns_empty_on_error():
    respx.get("https://www.reddit.com/r/funny/comments/abc123.json").mock(return_value=Response(500))
    comments = await fetch_top_comments(CONFIG, SAMPLE_POST)
    assert comments == []


@respx.mock
async def test_fetch_top_comments_uses_proxy():
    respx.get("https://proxy.example.com/r/funny/comments/abc123.json").mock(
        return_value=Response(200, json=COMMENTS_FIXTURE)
    )
    comments = await fetch_top_comments(CONFIG_WITH_PROXY, SAMPLE_POST)
    assert len(comments) == 1
    request = respx.calls.last.request
    assert request.headers["X-Proxy-Secret"] == "mysecret"


@respx.mock
async def test_fetch_top_comments_sorted_by_score():
    fixture = [
        {"data": {"children": []}},
        {
            "data": {
                "children": [
                    {"kind": "t1", "data": {"author": "low", "body": "low", "score": 1, "stickied": False}},
                    {"kind": "t1", "data": {"author": "high", "body": "high", "score": 999, "stickied": False}},
                ]
            }
        },
    ]
    respx.get("https://www.reddit.com/r/funny/comments/abc123.json").mock(return_value=Response(200, json=fixture))
    comments = await fetch_top_comments(CONFIG, SAMPLE_POST)
    assert comments[0]["author"] == "high"


# --- fetch_fresh_hls_url ---

HLS_POST_FIXTURE = [
    {
        "data": {
            "children": [
                {"data": {"media": {"reddit_video": {"hls_url": "https://v.redd.it/abc/HLSPlaylist.m3u8?a=newtoken"}}}}
            ]
        }
    }
]


@respx.mock
async def test_fetch_fresh_hls_url_returns_url():
    respx.get("https://www.reddit.com/comments/abc123.json").mock(return_value=Response(200, json=HLS_POST_FIXTURE))
    result = await fetch_fresh_hls_url(CONFIG, "t3_abc123")
    assert result == "https://v.redd.it/abc/HLSPlaylist.m3u8?a=newtoken"


@respx.mock
async def test_fetch_fresh_hls_url_uses_proxy():
    respx.get("https://proxy.example.com/comments/abc123.json").mock(return_value=Response(200, json=HLS_POST_FIXTURE))
    result = await fetch_fresh_hls_url(CONFIG_WITH_PROXY, "t3_abc123")
    assert result is not None
    request = respx.calls.last.request
    assert request.headers["X-Proxy-Secret"] == "mysecret"


@respx.mock
async def test_fetch_fresh_hls_url_returns_none_on_failure():
    respx.get("https://www.reddit.com/comments/abc123.json").mock(return_value=Response(500))
    result = await fetch_fresh_hls_url(CONFIG, "t3_abc123")
    assert result is None


@respx.mock
async def test_fetch_fresh_hls_url_returns_none_when_no_video():
    fixture = [{"data": {"children": [{"data": {"media": None}}]}}]
    respx.get("https://www.reddit.com/comments/abc123.json").mock(return_value=Response(200, json=fixture))
    result = await fetch_fresh_hls_url(CONFIG, "t3_abc123")
    assert result is None
