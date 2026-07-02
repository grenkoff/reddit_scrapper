from pathlib import Path

import httpx
import respx
from httpx import Response

from src.config import Config
from src.scraper.reddit import (
    _fetch_gallery_images,
    _fetch_preview_image,
    _fetch_selftext,
    _select_preview_image,
    _select_selftext,
    fetch_fresh_hls_url,
    fetch_top_comments,
    fetch_top_posts,
)

LISTING_HTML = Path("tests/fixtures/old_reddit_top.html").read_text()
COMMENTS_HTML = Path("tests/fixtures/old_reddit_comments.html").read_text()

CONFIG = Config(telegram_bot_token="test", telegram_chat_id="test", database_url="postgresql://test", posts_limit=25)

SAMPLE_POST = {
    "reddit_id": "t3_abc123",
    "subreddit": "funny",
    "url": "https://reddit.com/r/funny/comments/abc123/x/",
}
COMMENTS_URL = "https://old.reddit.com/r/funny/comments/abc123/x/"


# --- fetch_top_posts ---


@respx.mock
async def test_fetch_top_posts_skips_promoted_and_deleted():
    respx.get("https://old.reddit.com/top/").mock(return_value=Response(200, html=LISTING_HTML))
    posts = await fetch_top_posts(CONFIG)
    ids = {p["reddit_id"] for p in posts}
    assert "t3_ad1" not in ids
    assert "t3_del1" not in ids


# --- fetch_top_comments ---


@respx.mock
async def test_fetch_top_comments_parses_and_sorts():
    respx.get(COMMENTS_URL).mock(return_value=Response(200, html=COMMENTS_HTML))
    comments = await fetch_top_comments(CONFIG, SAMPLE_POST)
    scores = [c["score"] for c in comments]
    assert scores == sorted(scores, reverse=True)
    assert comments[0]["author"] == "high"
    assert comments[0]["score"] == 999


@respx.mock
async def test_fetch_top_comments_filters_deleted_stickied_removed():
    respx.get(COMMENTS_URL).mock(return_value=Response(200, html=COMMENTS_HTML))
    comments = await fetch_top_comments(CONFIG, SAMPLE_POST)
    authors = [c["author"] for c in comments]
    bodies = [c["body"] for c in comments]
    assert "[deleted]" not in authors
    assert "moderator" not in authors  # stickied
    assert "[removed]" not in bodies


@respx.mock
async def test_fetch_top_comments_ignores_nested_replies():
    respx.get(COMMENTS_URL).mock(return_value=Response(200, html=COMMENTS_HTML))
    comments = await fetch_top_comments(CONFIG, SAMPLE_POST)
    assert "nestedguy" not in [c["author"] for c in comments]


@respx.mock
async def test_fetch_top_comments_returns_empty_on_error():
    respx.get(COMMENTS_URL).mock(return_value=Response(500))
    assert await fetch_top_comments(CONFIG, SAMPLE_POST) == []


@respx.mock
async def test_fetch_top_comments_extracts_image_media():
    respx.get(COMMENTS_URL).mock(return_value=Response(200, html=COMMENTS_HTML))
    comments = await fetch_top_comments(CONFIG, SAMPLE_POST)
    img = next(c for c in comments if c["author"] == "imguser")
    assert img["media_type"] == "image"
    assert img["media_url"] == "https://preview.redd.it/img1.png?width=1170&s=SIGIMG"
    assert img["body"] == ""  # the literal <image> placeholder is stripped


@respx.mock
async def test_fetch_top_comments_extracts_gif_media():
    respx.get(COMMENTS_URL).mock(return_value=Response(200, html=COMMENTS_HTML))
    comments = await fetch_top_comments(CONFIG, SAMPLE_POST)
    gif = next(c for c in comments if c["author"] == "gifuser")
    assert gif["media_type"] == "gif"
    assert gif["media_url"] == "https://external-preview.redd.it/gif1.gif?width=200&s=SIGGIF"


@respx.mock
async def test_fetch_top_comments_media_with_text_kept():
    respx.get(COMMENTS_URL).mock(return_value=Response(200, html=COMMENTS_HTML))
    comments = await fetch_top_comments(CONFIG, SAMPLE_POST)
    mixed = next(c for c in comments if c["author"] == "mixeduser")
    assert mixed["media_url"] == "https://preview.redd.it/img2.jpeg?s=SIGMIX"
    assert mixed["body"] == "nice pic"


@respx.mock
async def test_fetch_top_comments_text_only_has_no_media():
    respx.get(COMMENTS_URL).mock(return_value=Response(200, html=COMMENTS_HTML))
    comments = await fetch_top_comments(CONFIG, SAMPLE_POST)
    high = next(c for c in comments if c["author"] == "high")
    assert high["media_url"] is None
    assert high["media_type"] is None


# --- _fetch_gallery_images ---


@respx.mock
async def test_fetch_gallery_images_scopes_to_tiles_and_picks_largest_signed():
    # Only the gallery tiles count: the largest signed width per image wins, unsigned variants
    # are skipped, and images from comments / "read next" / thumbnail must NOT leak in.
    page_html = (
        '<div class="sitetable linklisting"><div class="thing">'
        '<div class="media-preview-content">'
        '<a class="gallery-item-thumbnail-link" href="https://preview.redd.it/aaa.jpg?width=1170&amp;s=BIG">'
        '<img class="gallery-tile-content" src="https://preview.redd.it/aaa.jpg?width=108&amp;s=SMALL"/></a>'
        '<a class="gallery-item-thumbnail-link" href="https://preview.redd.it/unsigned.jpg?width=140">'
        '<img class="gallery-tile-content" src="https://preview.redd.it/bbb.jpg?width=960&amp;s=B"/></a>'
        "</div></div></div>"
        '<a class="thumbnail"><img src="https://preview.redd.it/THUMB.jpg?s=T"/></a>'
        '<div class="commentarea"><div class="sitetable"><div class="comment"><div class="md">'
        '<p><a href="https://preview.redd.it/COMMENT.jpg?s=CMT">&lt;image&gt;</a></p></div></div></div></div>'
    )
    respx.get(COMMENTS_URL).mock(return_value=Response(200, html=page_html))
    async with httpx.AsyncClient() as client:
        urls = await _fetch_gallery_images(client, SAMPLE_POST)
    assert urls == [
        "https://preview.redd.it/aaa.jpg?width=1170&s=BIG",
        "https://preview.redd.it/bbb.jpg?width=960&s=B",
    ]
    joined = " ".join(urls)
    assert "COMMENT.jpg" not in joined
    assert "THUMB.jpg" not in joined
    assert "unsigned.jpg" not in joined


@respx.mock
async def test_fetch_gallery_images_none_on_error():
    respx.get(COMMENTS_URL).mock(return_value=Response(500))
    async with httpx.AsyncClient() as client:
        assert await _fetch_gallery_images(client, SAMPLE_POST) is None


# --- _select_selftext / _fetch_selftext ---

POST_PAGE_HTML = (
    '<div class="content">'
    '<div class="sitetable"><div class="thing id-t3_txt self" data-fullname="t3_txt">'
    '<div class="entry"><div class="expando"><div class="usertext-body"><div class="md">'
    "<p>Recovered body line one.</p><p>Line two.</p>"
    "</div></div></div></div></div></div>"
    '<div class="commentarea"><div class="sitetable"><div class="comment" data-fullname="t1_c1">'
    '<div class="usertext-body"><div class="md"><p>a comment body must be ignored</p></div></div>'
    "</div></div></div>"
    "</div>"
)


def test_select_selftext_scoped_to_post_thing():
    body = _select_selftext(POST_PAGE_HTML, "t3_txt")
    assert body == "Recovered body line one.\nLine two."


def test_select_selftext_ignores_comment_bodies():
    body = _select_selftext(POST_PAGE_HTML, "t3_txt")
    assert "comment body" not in (body or "")


def test_select_selftext_none_when_thing_absent():
    assert _select_selftext(POST_PAGE_HTML, "t3_missing") is None


@respx.mock
async def test_fetch_selftext_returns_body():
    post = {"reddit_id": "t3_txt", "url": "https://reddit.com/r/x/comments/txt/t/"}
    respx.get("https://old.reddit.com/r/x/comments/txt/t/").mock(return_value=Response(200, html=POST_PAGE_HTML))
    async with httpx.AsyncClient() as client:
        assert await _fetch_selftext(client, post) == "Recovered body line one.\nLine two."


@respx.mock
async def test_fetch_selftext_none_on_error():
    post = {"reddit_id": "t3_txt", "url": "https://reddit.com/r/x/comments/txt/t/"}
    respx.get("https://old.reddit.com/r/x/comments/txt/t/").mock(return_value=Response(500))
    async with httpx.AsyncClient() as client:
        assert await _fetch_selftext(client, post) is None


@respx.mock
async def test_fetch_top_posts_recovers_missing_selftext_from_post_page():
    # A self post whose listing HTML carries no body — the scraper must refetch the post page.
    listing = (
        '<div class="thing id-t3_txt self" data-fullname="t3_txt" data-author="carol"'
        ' data-subreddit="AskReddit" data-url="https://www.reddit.com/r/AskReddit/comments/txt/q/"'
        ' data-domain="self.AskReddit" data-permalink="/r/AskReddit/comments/txt/q/"'
        ' data-score="10" data-comments-count="1" data-timestamp="1700000000000">'
        '<p class="title"><a class="title" href="#">Just a title</a></p>'
        "</div>"
    )
    respx.get("https://old.reddit.com/top/").mock(return_value=Response(200, html=listing))
    respx.get("https://old.reddit.com/r/AskReddit/comments/txt/q/").mock(
        return_value=Response(200, html=POST_PAGE_HTML)
    )
    posts = await fetch_top_posts(CONFIG)
    txt = next(p for p in posts if p["reddit_id"] == "t3_txt")
    assert txt["selftext"] == "Recovered body line one.\nLine two."


@respx.mock
async def test_fetch_top_posts_keeps_listing_selftext_without_refetch():
    # When the listing already has the body, no post-page request should be needed.
    route = respx.get("https://old.reddit.com/r/AskReddit/comments/text1/q/").mock(return_value=Response(500))
    respx.get("https://old.reddit.com/top/").mock(return_value=Response(200, html=LISTING_HTML))
    posts = await fetch_top_posts(CONFIG)
    txt = next(p for p in posts if p["reddit_id"] == "t3_text1")
    assert txt["selftext"] == "This is the body of the text post."
    assert not route.called


# --- _select_preview_image / _fetch_preview_image ---

PREVIEW_PAGE_HTML = (
    "<html><head>"
    '<meta property="og:image" content="https://external-preview.redd.it/big.jpg?width=1080&amp;s=SIG"/>'
    "</head><body></body></html>"
)


def test_select_preview_image_reads_og_image():
    assert _select_preview_image(PREVIEW_PAGE_HTML) == "https://external-preview.redd.it/big.jpg?width=1080&s=SIG"


def test_select_preview_image_rejects_default_reddit_icon():
    html_icon = '<meta property="og:image" content="https://www.redditstatic.com/icon.png"/>'
    assert _select_preview_image(html_icon) is None


def test_select_preview_image_none_when_absent():
    assert _select_preview_image("<html><head></head></html>") is None


@respx.mock
async def test_fetch_preview_image_returns_url():
    post = {"reddit_id": "t3_lnk", "url": "https://reddit.com/r/x/comments/lnk/l/"}
    respx.get("https://old.reddit.com/r/x/comments/lnk/l/").mock(return_value=Response(200, html=PREVIEW_PAGE_HTML))
    async with httpx.AsyncClient() as client:
        assert await _fetch_preview_image(client, post) == "https://external-preview.redd.it/big.jpg?width=1080&s=SIG"


@respx.mock
async def test_fetch_preview_image_none_on_error():
    post = {"reddit_id": "t3_lnk", "url": "https://reddit.com/r/x/comments/lnk/l/"}
    respx.get("https://old.reddit.com/r/x/comments/lnk/l/").mock(return_value=Response(500))
    async with httpx.AsyncClient() as client:
        assert await _fetch_preview_image(client, post) is None


@respx.mock
async def test_fetch_top_posts_upgrades_link_thumbnail_to_og_image():
    # A link post whose listing carries only a tiny thumbnail — the scraper must refetch the
    # post page and replace preview_url with the full-size og:image.
    listing = (
        '<div class="thing id-t3_lnk link" data-fullname="t3_lnk" data-author="erin"'
        ' data-subreddit="news" data-url="https://example.com/article" data-domain="example.com"'
        ' data-permalink="/r/news/comments/lnk/headline/"'
        ' data-score="321" data-comments-count="89" data-timestamp="1700000000000">'
        '<a class="thumbnail" href="#"><img src="//b.thumbs.redditmedia.com/tiny.jpg"></a>'
        '<a class="title" href="#">An external article</a>'
        "</div>"
    )
    respx.get("https://old.reddit.com/top/").mock(return_value=Response(200, html=listing))
    respx.get("https://old.reddit.com/r/news/comments/lnk/headline/").mock(
        return_value=Response(200, html=PREVIEW_PAGE_HTML)
    )
    posts = await fetch_top_posts(CONFIG)
    lnk = next(p for p in posts if p["reddit_id"] == "t3_lnk")
    assert lnk["preview_url"] == "https://external-preview.redd.it/big.jpg?width=1080&s=SIG"


@respx.mock
async def test_fetch_top_posts_keeps_thumbnail_when_og_image_missing():
    # If the post page has no usable og:image, keep the listing thumbnail as a fallback.
    listing = (
        '<div class="thing id-t3_lnk link" data-fullname="t3_lnk" data-author="erin"'
        ' data-subreddit="news" data-url="https://example.com/article" data-domain="example.com"'
        ' data-permalink="/r/news/comments/lnk/headline/"'
        ' data-score="321" data-comments-count="89" data-timestamp="1700000000000">'
        '<a class="thumbnail" href="#"><img src="//b.thumbs.redditmedia.com/tiny.jpg"></a>'
        '<a class="title" href="#">An external article</a>'
        "</div>"
    )
    respx.get("https://old.reddit.com/top/").mock(return_value=Response(200, html=listing))
    respx.get("https://old.reddit.com/r/news/comments/lnk/headline/").mock(
        return_value=Response(200, html="<html><head></head></html>")
    )
    posts = await fetch_top_posts(CONFIG)
    lnk = next(p for p in posts if p["reddit_id"] == "t3_lnk")
    assert lnk["preview_url"] == "https://b.thumbs.redditmedia.com/tiny.jpg"


# --- fetch_fresh_hls_url ---


async def test_fetch_fresh_hls_url_returns_none():
    # HLS is now a static derived path, so there is nothing to refresh.
    assert await fetch_fresh_hls_url(CONFIG, "t3_abc123") is None
