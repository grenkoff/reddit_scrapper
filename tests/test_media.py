import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import respx
from httpx import Response

from src.scraper.media import _get_duration, _get_ffmpeg, cleanup, download_image

# --- _get_ffmpeg ---


def test_get_ffmpeg_uses_imageio_when_available():
    with patch("imageio_ffmpeg.get_ffmpeg_exe", return_value="/fake/ffmpeg"):
        result = _get_ffmpeg()
    assert result == "/fake/ffmpeg"


def test_get_ffmpeg_falls_back_to_system():
    with (
        patch("imageio_ffmpeg.get_ffmpeg_exe", side_effect=Exception("not found")),
        patch("shutil.which", return_value="/usr/bin/ffmpeg"),
    ):
        result = _get_ffmpeg()
    assert result == "/usr/bin/ffmpeg"


def test_get_ffmpeg_returns_none_when_neither_available():
    with (
        patch("imageio_ffmpeg.get_ffmpeg_exe", side_effect=Exception("not found")),
        patch("shutil.which", return_value=None),
    ):
        result = _get_ffmpeg()
    assert result is None


# --- _get_duration ---


def _fake_run(cmd, capture_output, text):
    result = MagicMock()
    result.stderr = "Duration: 00:01:23.45, start: 0.000000"
    return result


def test_get_duration_parses_correctly():
    with patch("subprocess.run", side_effect=_fake_run):
        duration = _get_duration("/fake/ffmpeg", Path("video.mp4"))
    assert abs(duration - 83.45) < 0.01


def test_get_duration_returns_none_on_no_match():
    fake = MagicMock()
    fake.stderr = "No duration info here"
    with patch("subprocess.run", return_value=fake):
        duration = _get_duration("/fake/ffmpeg", Path("video.mp4"))
    assert duration is None


# --- cleanup ---


def test_cleanup_deletes_file():
    with tempfile.NamedTemporaryFile(delete=False) as f:
        path = Path(f.name)
    assert path.exists()
    cleanup(path)
    assert not path.exists()


def test_cleanup_silently_ignores_missing_file():
    path = Path("/tmp/definitely_does_not_exist_xyz.mp4")
    cleanup(path)  # should not raise


# --- download_video_direct (unit) ---


MPD = Path("tests/fixtures/reddit_video.mpd").read_bytes()
MPD_URL = "https://v.redd.it/abc/DASHPlaylist.mpd"


@respx.mock
async def test_download_video_direct_fallback_resolves_stream_from_manifest():
    """Stream names can't be guessed (Reddit renamed DASH_720.mp4 to CMAF_*), so read the manifest."""
    respx.get(MPD_URL).mock(return_value=Response(200, content=MPD))
    stream = respx.get("https://v.redd.it/abc/CMAF_720.mp4").mock(return_value=Response(200, content=b"video"))
    with patch("src.scraper.media._ffmpeg_dir_for_ytdlp", return_value=None):
        from src.scraper.media import download_video_direct

        # A legacy stored URL still resolves: only the v.redd.it id is taken from it.
        result = await download_video_direct("https://v.redd.it/abc/DASH_720.mp4")
    assert stream.called
    assert result is not None and result.read_bytes() == b"video"
    cleanup(result)


@respx.mock
async def test_download_video_direct_none_when_manifest_missing():
    respx.get(MPD_URL).mock(return_value=Response(403))
    with patch("src.scraper.media._ffmpeg_dir_for_ytdlp", return_value=None):
        from src.scraper.media import download_video_direct

        assert await download_video_direct("https://v.redd.it/abc/DASHPlaylist.mpd") is None


# --- _best_dash_video_url ---


@respx.mock
async def test_best_dash_video_url_picks_tallest_video_not_audio():
    import httpx

    from src.scraper.media import _best_dash_video_url

    respx.get(MPD_URL).mock(return_value=Response(200, content=MPD))
    async with httpx.AsyncClient() as client:
        assert await _best_dash_video_url(client, MPD_URL) == "https://v.redd.it/abc/CMAF_720.mp4"


@respx.mock
async def test_best_dash_video_url_raises_without_video_stream():
    import httpx
    import pytest

    from src.scraper.media import _best_dash_video_url

    audio_only = MPD.replace(b'contentType="video"', b'contentType="text"').replace(b'mimeType="video/mp4"', b"")
    respx.get(MPD_URL).mock(return_value=Response(200, content=audio_only))
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError):
            await _best_dash_video_url(client, MPD_URL)


# --- _ffmpeg_dir_for_ytdlp ---


def test_ffmpeg_dir_replaces_dangling_link(tmp_path, monkeypatch):
    """A link left by a host run points at a path the container lacks; it must be replaced, not trusted."""
    import src.scraper.media as media

    real = tmp_path / "ffmpeg-real"
    real.write_text("")
    link_dir = tmp_path / "links"
    link_dir.mkdir()
    (link_dir / "ffmpeg").symlink_to("/nonexistent/host/.venv/ffmpeg")
    monkeypatch.setattr(media, "_FFMPEG_LINK_DIR", link_dir)
    monkeypatch.setattr(media, "_get_ffmpeg", lambda: str(real))

    assert media._ffmpeg_dir_for_ytdlp() == str(link_dir)
    assert (link_dir / "ffmpeg").resolve() == real


def test_ffmpeg_dir_keeps_correct_link(tmp_path, monkeypatch):
    import src.scraper.media as media

    real = tmp_path / "ffmpeg-real"
    real.write_text("")
    link_dir = tmp_path / "links"
    monkeypatch.setattr(media, "_FFMPEG_LINK_DIR", link_dir)
    monkeypatch.setattr(media, "_get_ffmpeg", lambda: str(real))

    assert media._ffmpeg_dir_for_ytdlp() == str(link_dir)
    assert media._ffmpeg_dir_for_ytdlp() == str(link_dir)  # second call reuses the link
    assert (link_dir / "ffmpeg").resolve() == real


def test_ffmpeg_dir_none_without_ffmpeg(monkeypatch):
    import src.scraper.media as media

    monkeypatch.setattr(media, "_get_ffmpeg", lambda: None)
    assert media._ffmpeg_dir_for_ytdlp() is None


# --- download_image ---


@respx.mock
async def test_download_image_sends_browser_user_agent():
    route = respx.get("https://external-preview.redd.it/big.jpg").mock(
        return_value=Response(200, content=b"\xff\xd8\xff imagebytes")
    )
    path = await download_image("https://external-preview.redd.it/big.jpg?width=1080&s=SIG")
    assert path is not None
    ua = route.calls.last.request.headers.get("user-agent", "")
    assert "Mozilla" in ua and "Chrome" in ua
    cleanup(path)


@respx.mock
async def test_download_image_returns_none_on_403():
    respx.get("https://external-preview.redd.it/blocked.jpg").mock(return_value=Response(403))
    path = await download_image("https://external-preview.redd.it/blocked.jpg")
    assert path is None


async def test_download_video_direct_returns_none_on_failure():
    with (
        patch("src.scraper.media._ffmpeg_dir_for_ytdlp", return_value=None),
        patch("httpx.AsyncClient") as mock_client_cls,
    ):
        mock_client = MagicMock()

        async def fake_get(url, **kwargs):
            raise Exception("connection error")

        mock_client.get = fake_get
        mock_client.__aenter__ = lambda s: s
        mock_client.__aexit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        from src.scraper.media import download_video_direct

        result = await download_video_direct("https://v.redd.it/abc/DASH_720.mp4")
    assert result is None
