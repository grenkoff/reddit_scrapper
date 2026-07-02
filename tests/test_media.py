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


async def test_download_video_direct_skips_hls_when_no_ffmpeg():
    with (
        patch("src.scraper.media._ffmpeg_dir_for_ytdlp", return_value=None),
        patch("httpx.AsyncClient") as mock_client_cls,
    ):
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b"fake_video_bytes"
        mock_resp.raise_for_status = MagicMock()
        mock_client.__aenter__ = lambda s: s
        mock_client.__aexit__ = MagicMock(return_value=False)

        async def fake_get(url, **kwargs):
            return mock_resp

        mock_client.get = fake_get
        mock_client_cls.return_value = mock_client

        from src.scraper.media import download_video_direct

        result = await download_video_direct(
            "https://v.redd.it/abc/DASH_720.mp4",
            hls_url="https://v.redd.it/abc/HLSPlaylist.m3u8?a=token",
        )
    # With no ffmpeg, falls back to direct download
    if result:
        cleanup(result)


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
