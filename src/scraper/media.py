import asyncio
import logging
import os
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx
import yt_dlp

logger = logging.getLogger(__name__)

TMP_DIR = Path("tmp")
# Reddit's preview hosts (external-preview.redd.it) 403 non-browser clients, so present as one.
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


async def download_image(url: str) -> Path | None:
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(url, headers={"User-Agent": BROWSER_UA})
            response.raise_for_status()

        suffix = Path(url.split("?")[0]).suffix or ".jpg"
        TMP_DIR.mkdir(exist_ok=True)  # noqa: ASYNC240
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir=TMP_DIR) as tmp_file:
            tmp_file.write(response.content)
            return Path(tmp_file.name)
    except Exception:
        logger.warning("Failed to download image: %s", url, exc_info=True)
        return None


def _convert_gif_to_mp4(gif_path: Path) -> Path:
    """Convert a .gif file to .mp4 so Telegram plays it inline."""
    ffmpeg = _get_ffmpeg()
    if not ffmpeg:
        return gif_path
    try:
        mp4_path = gif_path.with_suffix(".mp4")
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-i",
                str(gif_path),
                "-movflags",
                "+faststart",
                "-pix_fmt",
                "yuv420p",
                "-vf",
                "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                "-an",
                str(mp4_path),
            ],
            capture_output=True,
            check=True,
        )
        gif_path.unlink(missing_ok=True)
        return mp4_path
    except Exception:
        logger.warning("GIF→MP4 conversion failed, using original: %s", gif_path, exc_info=True)
        return gif_path


async def download_gif(url: str) -> Path | None:
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(url)
            response.raise_for_status()

        suffix = Path(url.split("?")[0]).suffix or ".mp4"
        TMP_DIR.mkdir(exist_ok=True)  # noqa: ASYNC240
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir=TMP_DIR) as tmp_file:
            tmp_file.write(response.content)
            path = Path(tmp_file.name)

        if path.suffix.lower() == ".gif":
            path = await asyncio.get_event_loop().run_in_executor(None, _convert_gif_to_mp4, path)

        return path
    except Exception:
        logger.warning("Failed to download gif: %s", url, exc_info=True)
        return None


def _get_ffmpeg() -> str | None:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    import shutil

    return shutil.which("ffmpeg")


# yt-dlp wants a *directory* holding a binary named exactly "ffmpeg", while imageio-ffmpeg ships a
# versioned filename, so the binary is linked under that name. The link lives in the process's own
# temp dir rather than TMP_DIR on purpose: TMP_DIR is bind-mounted from the host, so a link created
# by a bot run on the host pointed into the host's .venv and dangled inside the container — which
# silently switched off every Reddit video download for weeks.
_FFMPEG_LINK_DIR = Path(tempfile.gettempdir()) / "reddit-scrapper-ffmpeg"


def _ffmpeg_dir_for_ytdlp() -> str | None:
    """Return a directory containing a symlink named 'ffmpeg' for yt-dlp."""
    real_bin = _get_ffmpeg()
    if not real_bin:
        logger.warning("ffmpeg not found — Reddit videos will fall back to a silent stream")
        return None
    link = _FFMPEG_LINK_DIR / "ffmpeg"
    try:
        _FFMPEG_LINK_DIR.mkdir(parents=True, exist_ok=True)
        # is_symlink(), not exists(): exists() follows the link, so a dangling one reads as absent
        # and recreating it then fails with FileExistsError.
        if link.is_symlink() and os.readlink(link) != real_bin:
            link.unlink()
        if not link.is_symlink():
            link.symlink_to(real_bin)
        return str(_FFMPEG_LINK_DIR)
    except OSError:
        logger.warning(
            "Could not link ffmpeg for yt-dlp — Reddit videos will fall back to a silent stream", exc_info=True
        )
        return None


_DASH_NS = "{urn:mpeg:dash:schema:mpd:2011}"


async def _best_dash_video_url(client: httpx.AsyncClient, video_url: str) -> str:
    """Resolve a v.redd.it video to its tallest real video stream via the DASH manifest.

    Stream filenames can't be guessed: Reddit renamed ``DASH_720.mp4`` to ``CMAF_480.mp4`` and
    friends, and each video carries its own set of heights (many top out below 720p). The manifest
    at ``https://v.redd.it/<id>/DASHPlaylist.mpd`` lists what actually exists.
    """
    base = video_url.split("?", 1)[0].rstrip("/")
    if base.endswith((".mpd", ".mp4", ".m3u8")):
        base = base.rsplit("/", 1)[0]
    response = await client.get(f"{base}/DASHPlaylist.mpd", headers={"User-Agent": BROWSER_UA})
    response.raise_for_status()

    best_height, best_name = -1, None
    for adaptation in ET.fromstring(response.content).iter(f"{_DASH_NS}AdaptationSet"):
        for rep in adaptation.iter(f"{_DASH_NS}Representation"):
            mime = rep.get("mimeType") or adaptation.get("mimeType") or ""
            if adaptation.get("contentType") != "video" and not mime.startswith("video/"):
                continue
            name = rep.findtext(f"{_DASH_NS}BaseURL")
            height = int(rep.get("height") or 0)
            if name and height > best_height:
                best_height, best_name = height, name.strip()
    if not best_name:
        raise ValueError(f"no video stream in DASH manifest for {base}")
    return f"{base}/{best_name}"


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
                TMP_DIR.mkdir(exist_ok=True)  # noqa: ASYNC240
                output_template = str(TMP_DIR / "%(id)s.%(ext)s")
                ydl_opts = {
                    "outtmpl": output_template,
                    "format": "bestvideo[filesize<40M]+bestaudio/best[filesize<40M]/bestvideo+bestaudio/best",
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
        TMP_DIR.mkdir(exist_ok=True)  # noqa: ASYNC240
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            if "v.redd.it" in url:
                url = await _best_dash_video_url(client, url)
            video_resp = await client.get(url, headers={"User-Agent": BROWSER_UA})
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
            "format": "bestvideo[ext=mp4][filesize<40M]+bestaudio/best[ext=mp4][filesize<40M]/best",
            "merge_output_format": "mp4",
            "quiet": True,
            "no_warnings": True,
            "ffmpeg_location": _ffmpeg_dir_for_ytdlp(),
            "http_headers": {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            },
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            return Path(filename)
    except Exception:
        logger.warning("Failed to download video: %s", url, exc_info=True)
        return None


def _get_duration(ffmpeg_bin: str, path: Path) -> float | None:
    """Parse video duration in seconds from ffmpeg stderr."""
    result = subprocess.run(
        [ffmpeg_bin, "-i", str(path)],
        capture_output=True,
        text=True,
    )
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", result.stderr)
    if m:
        h, mn, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
        return h * 3600 + mn * 60 + s
    return None


def compress_video(path: Path, max_mb: int = 49) -> Path | None:
    """Add faststart flag; re-encode and shrink if file exceeds max_mb.

    Returns None if the video cannot be brought under max_mb after all attempts.
    """
    ffmpeg = _get_ffmpeg()
    if not ffmpeg:
        return path

    try:
        TMP_DIR.mkdir(exist_ok=True)
        size_mb = path.stat().st_size / (1024 * 1024)

        if size_mb <= max_mb:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4", dir=TMP_DIR) as out_f:
                out_path = Path(out_f.name)
            subprocess.run(
                [ffmpeg, "-y", "-i", str(path), "-c", "copy", "-movflags", "+faststart", str(out_path)],
                capture_output=True,
                check=True,
            )
            path.unlink(missing_ok=True)
            logger.info("Video faststart: %.1f MB", out_path.stat().st_size / (1024 * 1024))
            return out_path

        duration = _get_duration(ffmpeg, path)
        if not duration or duration <= 0:
            logger.warning("Cannot determine video duration, skipping video: %s", path)
            path.unlink(missing_ok=True)
            return None

        audio_kbps = 96
        # Try three progressively lower bitrates (100%, 70%, 50% of theoretical target)
        for scale in [1.0, 0.7, 0.5]:
            target_kbps = max(int((max_mb * 8 * 1024 / duration) * scale) - audio_kbps, 150)
            with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4", dir=TMP_DIR) as out_f:
                out_path = Path(out_f.name)
            cmd = [
                ffmpeg,
                "-y",
                "-i",
                str(path),
                "-c:v",
                "libx264",
                "-b:v",
                f"{target_kbps}k",
                "-maxrate",
                f"{int(target_kbps * 1.2)}k",
                "-bufsize",
                f"{target_kbps * 2}k",
                "-preset",
                "fast",
                "-movflags",
                "+faststart",
                "-c:a",
                "aac",
                "-b:a",
                f"{audio_kbps}k",
                str(out_path),
            ]
            subprocess.run(cmd, capture_output=True, check=True)
            out_mb = out_path.stat().st_size / (1024 * 1024)
            logger.info("Video compress (scale=%.1f): %.1f MB → %.1f MB", scale, size_mb, out_mb)
            if out_mb <= max_mb:
                path.unlink(missing_ok=True)
                return out_path
            out_path.unlink(missing_ok=True)

        logger.warning("Video still too large after all compression attempts (%.1f MB), skipping", size_mb)
        path.unlink(missing_ok=True)
        return None
    except Exception:
        logger.warning("Video compression failed: %s", path, exc_info=True)
        path.unlink(missing_ok=True)
        return None


def cleanup(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except Exception:
        logger.warning("Failed to delete tmp file: %s", path)
