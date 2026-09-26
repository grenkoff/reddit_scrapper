from pathlib import Path

import pytest
import respx
from bs4 import BeautifulSoup
from httpx import Response

from src.config import Config
from src.scraper.reddit import (
    _clean_comment_text,
    _extract_comment_media,
    _is_mod_announcement,
    _selftext_from_md,
    fetch_fresh_hls_url,
    fetch_top_comments,
    fetch_top_posts,
)


def _md(html: str) -> str | None:
    return _selftext_from_md(BeautifulSoup(f'<div class="md">{html}</div>', "html.parser").select_one("div.md"))


LISTING_RSS = Path("tests/fixtures/reddit_top.rss").read_bytes()
COMMENTS_RSS = Path("tests/fixtures/reddit_comments.rss").read_bytes()

CONFIG = Config(telegram_bot_token="test", telegram_chat_id="test", database_url="postgresql://test", posts_limit=25)

SAMPLE_POST = {
    "reddit_id": "t3_abc123",
    "subreddit": "funny",
    "url": "https://reddit.com/r/funny/comments/abc123/x/",
}
LISTING_URL = "https://www.reddit.com/r/all/top/.rss"
COMMENTS_URL = "https://www.reddit.com/r/funny/comments/abc123/x/.rss"


@pytest.fixture
async def comments():
    with respx.mock:
        respx.get(COMMENTS_URL).mock(return_value=Response(200, content=COMMENTS_RSS))
        return await fetch_top_comments(CONFIG, SAMPLE_POST)


# --- fetch_top_posts ---


@respx.mock
async def test_fetch_top_posts_skips_deleted():
    respx.get(LISTING_URL).mock(return_value=Response(200, content=LISTING_RSS))
    posts = await fetch_top_posts(CONFIG)
    assert "t3_del1" not in {p["reddit_id"] for p in posts}


@respx.mock
async def test_fetch_top_posts_requests_configured_limit():
    route = respx.get(LISTING_URL).mock(return_value=Response(200, content=LISTING_RSS))
    await fetch_top_posts(CONFIG)
    assert dict(route.calls[0].request.url.params) == {"t": "day", "limit": "25"}


# --- fetch_top_comments ---


async def test_fetch_top_comments_parses_in_feed_order(comments):
    # The feed exposes no comment scores, so its own order is what ranks them.
    assert [c["author"] for c in comments][:2] == ["first", "second"]
    assert comments[0]["body"] == "First comment here"


async def test_fetch_top_comments_skips_pinned_mod_announcement(comments):
    """Reddit pins a mod notice above the real comments and the feed gives it no marker."""
    assert "AutoModerator" not in [c["author"] for c in comments]
    assert comments[0]["author"] == "first"  # the readers' top comment still leads


async def test_fetch_top_comments_skips_the_post_entry(comments):
    # A post's feed leads with the post itself (t3_); only t1_ entries are comments.
    assert "op" not in [c["author"] for c in comments]


async def test_fetch_top_comments_filters_deleted_and_removed(comments):
    assert "[deleted]" not in [c["author"] for c in comments]
    assert "[removed]" not in [c["body"] for c in comments]


async def test_fetch_top_comments_score_absent_from_feed(comments):
    assert all(c["score"] == 0 for c in comments)


async def test_fetch_top_comments_respects_limit():
    with respx.mock:
        respx.get(COMMENTS_URL).mock(return_value=Response(200, content=COMMENTS_RSS))
        assert len(await fetch_top_comments(CONFIG, SAMPLE_POST, limit=2)) == 2


@respx.mock
async def test_fetch_top_comments_returns_empty_on_error():
    respx.get(COMMENTS_URL).mock(return_value=Response(500))
    assert await fetch_top_comments(CONFIG, SAMPLE_POST) == []


async def test_fetch_top_comments_extracts_gif_media(comments):
    gif = next(c for c in comments if c["author"] == "gifposter")
    assert gif["media_type"] == "gif"
    assert gif["media_url"] == "https://i.redd.it/emote.gif"


async def test_fetch_top_comments_media_with_text_kept(comments):
    mixed = next(c for c in comments if c["author"] == "imageposter")
    assert mixed["media_type"] == "image"
    assert mixed["media_url"] == "https://preview.redd.it/shot.png?s=sig"
    assert mixed["body"] == "look at this"  # the literal <image> placeholder is stripped


async def test_fetch_top_comments_text_only_has_no_media(comments):
    first = next(c for c in comments if c["author"] == "first")
    assert first["media_url"] is None
    assert first["media_type"] is None


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


async def test_fetch_fresh_hls_url_returns_none():
    # HLS is now a static derived path, so there is nothing to refresh.
    assert await fetch_fresh_hls_url(CONFIG, "t3_abc123") is None


# --- _is_mod_announcement (bodies below are real notices sampled from Reddit on 2026-09-23) ---


def test_mod_announcement_detects_bot_footer():
    body = (
        "This post has hit r/all or r/popular. Please keep this in mind when browsing the comments. "
        "I am a bot, and this action was performed automatically. "
        "Please contact the moderators of this subreddit if you have any questions or concerns."
    )
    assert _is_mod_announcement("trendingtattler", body) is True


def test_mod_announcement_detects_notice_without_bot_footer():
    body = (
        "\U0001f4cc PLEASE READ BEFORE COMMENTING This post is flaired Guest List Only. "
        "This means the conversation is being strictly moderated."
    )
    assert _is_mod_announcement("flairassistant", body) is True


def test_mod_announcement_detects_modteam_author():
    body = 'Unfortunately, your post has been removed because it violates our "No screens" rule.'
    assert _is_mod_announcement("mildlyinteresting-ModTeam", body) is True


def test_mod_announcement_detects_automoderator_whatever_the_body():
    assert _is_mod_announcement("AutoModerator", "Your submission is now live.") is True


def test_mod_announcement_leaves_ordinary_comments_alone():
    assert _is_mod_announcement("lightningandblunder", "Look at that, shit can actually get done.") is False
    assert _is_mod_announcement("some_user", "the mods here are strict but fair") is False


# --- top_level_only: replies must not reach the discussion group ---

SUBTREE = "https://www.reddit.com/r/funny/comments/abc123/x/{}/.rss"


def _feed(ids: list[str]) -> bytes:
    entries = "".join(f"<entry><id>{i}</id></entry>" for i in ids)
    return f'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">{entries}</feed>'.encode()


@respx.mock
async def test_top_level_only_skips_replies():
    """The feed flattens the thread, so each root comment's subtree marks which ids are replies."""
    respx.get(COMMENTS_URL).mock(return_value=Response(200, content=COMMENTS_RSS))
    respx.get(SUBTREE.format("sticky")).mock(return_value=Response(200, content=_feed(["t1_sticky"])))
    # u/second wrote a reply to u/first rather than a comment of its own.
    respx.get(SUBTREE.format("c1")).mock(return_value=Response(200, content=_feed(["t1_c1", "t1_c2"])))
    for cid in ("c3", "c4"):
        respx.get(SUBTREE.format(cid)).mock(return_value=Response(200, content=_feed([f"t1_{cid}"])))

    comments = await fetch_top_comments(CONFIG, SAMPLE_POST, limit=2, top_level_only=True)

    assert [c["author"] for c in comments] == ["first", "gifposter"]
    assert "second" not in [c["author"] for c in comments]


@respx.mock
async def test_flat_fetch_costs_a_single_request():
    """The explanation prompt only needs context, so it must not pay for subtree lookups."""
    route = respx.get(COMMENTS_URL).mock(return_value=Response(200, content=COMMENTS_RSS))
    subtree = respx.get(SUBTREE.format("c1")).mock(return_value=Response(200, content=_feed(["t1_c1"])))

    comments = await fetch_top_comments(CONFIG, SAMPLE_POST, limit=2)

    assert [c["author"] for c in comments] == ["first", "second"]
    assert route.call_count == 1
    assert not subtree.called


@respx.mock
async def test_unresolvable_subtree_stops_instead_of_guessing():
    """Publishing a reply as a top-level comment is worse than publishing fewer comments."""
    respx.get(COMMENTS_URL).mock(return_value=Response(200, content=COMMENTS_RSS))
    respx.get(SUBTREE.format("sticky")).mock(return_value=Response(200, content=_feed(["t1_sticky"])))
    respx.get(SUBTREE.format("c1")).mock(return_value=Response(500))

    comments = await fetch_top_comments(CONFIG, SAMPLE_POST, limit=5, top_level_only=True)

    assert [c["author"] for c in comments] == ["first"]


@respx.mock
async def test_replies_under_a_filtered_comment_are_skipped_too():
    """A mod notice is not published, but its replies are still replies."""
    respx.get(COMMENTS_URL).mock(return_value=Response(200, content=COMMENTS_RSS))
    # Everything below the pinned notice belongs to its thread.
    respx.get(SUBTREE.format("sticky")).mock(return_value=Response(200, content=_feed(["t1_sticky", "t1_c1", "t1_c2"])))
    respx.get(SUBTREE.format("c3")).mock(return_value=Response(200, content=_feed(["t1_c3"])))
    respx.get(SUBTREE.format("c4")).mock(return_value=Response(200, content=_feed(["t1_c4"])))

    comments = await fetch_top_comments(CONFIG, SAMPLE_POST, limit=1, top_level_only=True)

    assert [c["author"] for c in comments] == ["gifposter"]


# --- comment media (the feeds render a bare link as its own URL, not as <image>) ---


def _md_tag(inner: str):
    return BeautifulSoup(f'<div class="md">{inner}</div>', "html.parser").select_one("div.md")


def test_reddit_image_link_becomes_media_and_leaves_the_text():
    """Real shape from the feed: the anchor text is the URL, which used to be printed as text."""
    md = _md_tag(
        "<p>You will feel the wrath of flatulence</p>"
        '<p><a href="https://preview.redd.it/t7y.jpeg?width=271&amp;s=abc">'
        "https://preview.redd.it/t7y.jpeg?width=271&amp;s=abc</a></p>"
    )
    assert _extract_comment_media(md) == ("https://preview.redd.it/t7y.jpeg?width=271&s=abc", "image")
    assert _clean_comment_text(md) == "You will feel the wrath of flatulence"


def test_giphy_page_link_becomes_a_direct_gif():
    """The giphy page 403s for bots; the file behind it does not."""
    md = _md_tag('<p><a href="https://giphy.com/gifs/XQtLwTp17">https://giphy.com/gifs/XQtLwTp17</a></p>')
    assert _extract_comment_media(md) == ("https://media.giphy.com/media/XQtLwTp17/giphy.gif", "gif")
    assert _clean_comment_text(md) == ""  # nothing left to say, and nothing left to translate


def test_giphy_slug_keeps_only_the_id():
    md = _md_tag('<p><a href="https://giphy.com/gifs/cat-funny-AbC123">https://giphy.com/gifs/cat-funny-AbC123</a></p>')
    assert _extract_comment_media(md)[0] == "https://media.giphy.com/media/AbC123/giphy.gif"


def test_link_with_words_keeps_its_text():
    md = _md_tag('<p>see <a href="https://i.redd.it/x.jpg">this photo</a> now</p>')
    assert _extract_comment_media(md) == ("https://i.redd.it/x.jpg", "image")
    assert "this photo" in _clean_comment_text(md)  # the sentence is worth more than the URL


def test_ordinary_link_is_not_media():
    md = _md_tag('<p>source: <a href="https://nytimes.com/article">https://nytimes.com/article</a></p>')
    assert _extract_comment_media(md) == (None, None)
    assert "nytimes.com/article" in _clean_comment_text(md)


def test_legacy_image_placeholder_still_works():
    md = _md_tag('<p>nice <a href="https://preview.redd.it/y.png?s=1">&lt;image&gt;</a></p>')
    assert _extract_comment_media(md) == ("https://preview.redd.it/y.png?s=1", "image")
    assert _clean_comment_text(md) == "nice"


def test_emote_img_still_works():
    md = _md_tag('<p><img src="https://i.redd.it/emote.gif"/></p>')
    assert _extract_comment_media(md) == ("https://i.redd.it/emote.gif", "gif")
