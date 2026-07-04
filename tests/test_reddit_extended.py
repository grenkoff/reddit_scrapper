from pathlib import Path

import httpx
import respx
from bs4 import BeautifulSoup
from httpx import Response

from src.config import Config
from src.scraper.reddit import (
    _select_preview_image,
    _select_selftext,
    _selftext_from_md,
    enrich_post,
    fetch_fresh_hls_url,
    fetch_top_comments,
    fetch_top_posts,
)


def _md(html: str) -> str | None:
    return _selftext_from_md(BeautifulSoup(f'<div class="md">{html}</div>', "html.parser").select_one("div.md"))


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


# --- _select_selftext (page parser) ---

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

GALLERY_PAGE_HTML = (
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

PREVIEW_PAGE_HTML = (
    "<html><head>"
    '<meta property="og:image" content="https://external-preview.redd.it/big.jpg?width=1080&amp;s=SIG"/>'
    "</head><body></body></html>"
)


def test_select_selftext_scoped_to_post_thing():
    body = _select_selftext(POST_PAGE_HTML, "t3_txt")
    assert body == "Recovered body line one.\n\nLine two."


def test_select_selftext_ignores_comment_bodies():
    body = _select_selftext(POST_PAGE_HTML, "t3_txt")
    assert "comment body" not in (body or "")


def test_select_selftext_none_when_thing_absent():
    assert _select_selftext(POST_PAGE_HTML, "t3_missing") is None


# --- _selftext_from_md (rendered HTML -> markdown, restoring paragraphs/emphasis) ---


def test_md_paragraphs_separated_by_blank_line():
    assert _md("<p>First paragraph.</p><p>Second paragraph.</p>") == "First paragraph.\n\nSecond paragraph."


def test_md_soft_breaks_become_newlines():
    assert _md("<p>But.<br/>It.<br/>Ain't.</p>") == "But.\nIt.\nAin't."


def test_md_emphasis_and_links_become_markdown():
    body = _md('<p>An <strong>bold</strong> and <em>italic</em> and <a href="https://x.com">link</a>.</p>')
    assert body == "An **bold** and *italic* and [link](https://x.com)."


def test_md_blockquote_prefixes_lines():
    assert _md("<blockquote><p>quoted line</p></blockquote><p>after</p>") == "> quoted line\n\nafter"


def test_md_list_items_become_dashes():
    assert _md("<ul><li>one</li><li>two</li></ul>") == "- one\n- two"


def test_md_plain_text_without_tags():
    assert _md("just text") == "just text"


def test_select_preview_image_reads_og_image():
    assert _select_preview_image(PREVIEW_PAGE_HTML) == "https://external-preview.redd.it/big.jpg?width=1080&s=SIG"


def test_select_preview_image_rejects_default_reddit_icon():
    html_icon = '<meta property="og:image" content="https://www.redditstatic.com/icon.png"/>'
    assert _select_preview_image(html_icon) is None


def test_select_preview_image_none_when_absent():
    assert _select_preview_image("<html><head></head></html>") is None


# --- enrich_post (per-new-post page enrichment) ---


@respx.mock
async def test_enrich_recovers_body_for_media_post():
    # The core fix: an image post that carries a text body must get it from its page.
    post = {"reddit_id": "t3_txt", "url": "https://reddit.com/r/x/comments/txt/t/", "post_type": "image"}
    respx.get("https://old.reddit.com/r/x/comments/txt/t/").mock(return_value=Response(200, html=POST_PAGE_HTML))
    async with httpx.AsyncClient() as client:
        await enrich_post(client, post)
    assert post["selftext"] == "Recovered body line one.\n\nLine two."


@respx.mock
async def test_enrich_recovers_body_for_text_post():
    post = {"reddit_id": "t3_txt", "url": "https://reddit.com/r/x/comments/txt/t/", "post_type": "text"}
    respx.get("https://old.reddit.com/r/x/comments/txt/t/").mock(return_value=Response(200, html=POST_PAGE_HTML))
    async with httpx.AsyncClient() as client:
        await enrich_post(client, post)
    assert post["selftext"] == "Recovered body line one.\n\nLine two."


@respx.mock
async def test_enrich_body_none_on_page_error():
    post = {
        "reddit_id": "t3_txt",
        "url": "https://reddit.com/r/x/comments/txt/t/",
        "post_type": "image",
        "selftext": None,
    }
    respx.get("https://old.reddit.com/r/x/comments/txt/t/").mock(return_value=Response(500))
    async with httpx.AsyncClient() as client:
        await enrich_post(client, post)
    assert post["selftext"] is None


@respx.mock
async def test_enrich_skips_fetch_when_nothing_needed():
    # Body already present, not a gallery or link — no page request should happen.
    route = respx.get(COMMENTS_URL).mock(return_value=Response(500))
    post = {**SAMPLE_POST, "post_type": "image", "selftext": "already have it", "preview_url": None}
    async with httpx.AsyncClient() as client:
        await enrich_post(client, post)
    assert post["selftext"] == "already have it"
    assert not route.called


@respx.mock
async def test_enrich_gallery_fills_media_urls_scoped_to_tiles():
    # Largest signed width per image wins; unsigned variants and comment/thumbnail images excluded.
    post = {**SAMPLE_POST, "post_type": "gallery", "selftext": "x", "preview_url": None, "media_urls": None}
    respx.get(COMMENTS_URL).mock(return_value=Response(200, html=GALLERY_PAGE_HTML))
    async with httpx.AsyncClient() as client:
        await enrich_post(client, post)
    assert post["media_urls"] == [
        "https://preview.redd.it/aaa.jpg?width=1170&s=BIG",
        "https://preview.redd.it/bbb.jpg?width=960&s=B",
    ]
    joined = " ".join(post["media_urls"])
    assert "COMMENT.jpg" not in joined and "THUMB.jpg" not in joined and "unsigned.jpg" not in joined


@respx.mock
async def test_enrich_upgrades_link_thumbnail_to_og_image():
    post = {
        **SAMPLE_POST,
        "post_type": "link",
        "selftext": "x",
        "preview_url": "https://b.thumbs.redditmedia.com/tiny.jpg",
    }
    respx.get(COMMENTS_URL).mock(return_value=Response(200, html=PREVIEW_PAGE_HTML))
    async with httpx.AsyncClient() as client:
        await enrich_post(client, post)
    assert post["preview_url"] == "https://external-preview.redd.it/big.jpg?width=1080&s=SIG"


@respx.mock
async def test_enrich_keeps_thumbnail_when_og_image_missing():
    post = {
        **SAMPLE_POST,
        "post_type": "link",
        "selftext": "x",
        "preview_url": "https://b.thumbs.redditmedia.com/tiny.jpg",
    }
    respx.get(COMMENTS_URL).mock(return_value=Response(200, html="<html><head></head></html>"))
    async with httpx.AsyncClient() as client:
        await enrich_post(client, post)
    assert post["preview_url"] == "https://b.thumbs.redditmedia.com/tiny.jpg"


@respx.mock
async def test_enrich_link_gets_og_image_without_listing_thumbnail():
    # old.reddit may omit the listing thumbnail even when the post has a large preview:
    # enrich must still fetch the page and pull og:image, so preview_url isn't left empty.
    post = {**SAMPLE_POST, "post_type": "link", "selftext": "x", "preview_url": None}
    respx.get(COMMENTS_URL).mock(return_value=Response(200, html=PREVIEW_PAGE_HTML))
    async with httpx.AsyncClient() as client:
        await enrich_post(client, post)
    assert post["preview_url"] == "https://external-preview.redd.it/big.jpg?width=1080&s=SIG"


# --- fetch_fresh_hls_url ---


async def test_fetch_fresh_hls_url_returns_none():
    # HLS is now a static derived path, so there is nothing to refresh.
    assert await fetch_fresh_hls_url(CONFIG, "t3_abc123") is None
