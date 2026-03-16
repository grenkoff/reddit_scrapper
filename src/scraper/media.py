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
