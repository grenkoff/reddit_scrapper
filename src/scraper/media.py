import asyncio
import logging
import tempfile
from pathlib import Path

import httpx
import yt_dlp

logger = logging.getLogger(__name__)

TMP_DIR = Path("tmp")


async def download_image(url: str) -> Path | None:
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(url)
            response.raise_for_status()

        suffix = Path(url.split("?")[0]).suffix or ".jpg"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir=TMP_DIR) as tmp_file:
            tmp_file.write(response.content)
            return Path(tmp_file.name)
    except Exception:
        logger.warning("Failed to download image: %s", url, exc_info=True)
        return None


async def download_gif(url: str) -> Path | None:
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(url)
            response.raise_for_status()

        suffix = Path(url.split("?")[0]).suffix or ".mp4"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir=TMP_DIR) as tmp_file:
            tmp_file.write(response.content)
            return Path(tmp_file.name)
    except Exception:
        logger.warning("Failed to download gif: %s", url, exc_info=True)
        return None


def _get_ffmpeg() -> str | None:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _ffmpeg_dir_for_ytdlp() -> str | None:
    """Return a directory containing a symlink named 'ffmpeg' for yt-dlp."""
    real_bin = _get_ffmpeg()
    if not real_bin:
        return None
    try:
        symlink_dir = TMP_DIR / ".bin"
        symlink_dir.mkdir(parents=True, exist_ok=True)
        symlink = symlink_dir / "ffmpeg"
        if not symlink.exists():
            symlink.symlink_to(real_bin)
        return str(symlink_dir)
    except Exception:
        return None


async def download_video_direct(url: str, hls_url: str | None = None) -> Path | None:
    """Download Reddit-hosted video with audio.

    Tries yt-dlp on the HLS URL first (contains auth token → audio available),
    falls back to direct HTTP download of the video stream only.
    """
    if hls_url:
        ffmpeg_dir = _ffmpeg_dir_for_ytdlp()
        if ffmpeg_dir:
            try:
                logger.info("Downloading video with audio via HLS: %s", hls_url[:80])
                TMP_DIR.mkdir(exist_ok=True)
                output_template = str(TMP_DIR / "%(id)s.%(ext)s")
                ydl_opts = {
                    "outtmpl": output_template,
                    "format": "bestvideo+bestaudio/best",
                    "merge_output_format": "mp4",
                    "quiet": True,
                    "no_warnings": True,
                    "ffmpeg_location": ffmpeg_dir,
                }

                def _dl_hls() -> Path:
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(hls_url, download=True)
                        return Path(ydl.prepare_filename(info))

                return await asyncio.get_event_loop().run_in_executor(None, _dl_hls)
            except Exception:
                logger.warning("yt-dlp HLS download failed, falling back to direct: %s", hls_url, exc_info=True)

    # Fallback: direct download, video stream only (no audio)
    try:
        TMP_DIR.mkdir(exist_ok=True)
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            video_resp = await client.get(url)
            video_resp.raise_for_status()
            video_bytes = video_resp.content

        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4", dir=TMP_DIR) as tmp_file:
            tmp_file.write(video_bytes)
            return Path(tmp_file.name)

    except Exception:
        logger.warning("Failed to download video: %s", url, exc_info=True)
        return None


def download_video(url: str) -> Path | None:
    try:
        TMP_DIR.mkdir(exist_ok=True)
        output_template = str(TMP_DIR / "%(id)s.%(ext)s")
        ydl_opts = {
            "outtmpl": output_template,
            "format": "bestvideo[ext=mp4]+bestaudio/best[ext=mp4]/best",
            "merge_output_format": "mp4",
            "quiet": True,
            "no_warnings": True,
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            },
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            return Path(filename)
    except Exception:
        logger.warning("Failed to download video: %s", url, exc_info=True)
        return None


def cleanup(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except Exception:
        logger.warning("Failed to delete tmp file: %s", path)
