import io
import json
import logging
import statistics

import httpx
from PIL import Image, ImageDraw, ImageFont

from src.config import Config

logger = logging.getLogger(__name__)

_DETECT_PROMPT = (
    "Detect all text visible in this image and translate each piece to Russian. "
    "Return ONLY valid JSON with no markdown, no explanation, no code block:\n"
    '[{"text": "original text", "translation": "русский перевод", '
    '"box": [y_min, x_min, y_max, x_max]}]\n'
    "box values are integers normalized to 0–1000 "
    "(y_min/y_max = top/bottom distance from top, x_min/x_max = left/right distance from left). "
    "If there is no text in the image return exactly: []"
)


async def detect_image_text(image_url: str, config: Config) -> list[dict]:
    from src.explainer.gemini import _fetch_image

    image_part = _fetch_image(image_url)
    if not image_part:
        return []

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [image_part, {"text": _DETECT_PROMPT}],
            }
        ],
        "generationConfig": {
            "maxOutputTokens": 2048,
            "temperature": 0.1,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-3.1-flash-lite:generateContent?key={config.gemini_api_key}"
    )
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
        raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        # Strip markdown code block if model wraps response
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        regions = json.loads(raw)
        if not isinstance(regions, list):
            return []
        return [r for r in regions if _valid_region(r)]
    except Exception as e:
        logger.debug("Image text detection failed: %s", e)
        return []


def _valid_region(r: dict) -> bool:
    return (
        isinstance(r, dict)
        and "translation" in r
        and "box" in r
        and isinstance(r["box"], list)
        and len(r["box"]) == 4
    )


def _median_color(pixels: list[tuple]) -> tuple[int, int, int]:
    if not pixels:
        return (255, 255, 255)
    r = statistics.median(p[0] for p in pixels)
    g = statistics.median(p[1] for p in pixels)
    b = statistics.median(p[2] for p in pixels)
    return (int(r), int(g), int(b))


def _contrasting(color: tuple[int, int, int]) -> tuple[int, int, int]:
    luminance = 0.299 * color[0] + 0.587 * color[1] + 0.114 * color[2]
    return (0, 0, 0) if luminance > 128 else (255, 255, 255)


def _fit_text(draw: ImageDraw.ImageDraw, text: str, box_w: int, box_h: int) -> tuple[ImageFont.ImageFont, int, int]:
    for size in range(max(8, int(box_h * 0.4)), 7, -1):
        font = ImageFont.load_default(size=size)
        bbox = draw.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        if tw <= box_w and th <= box_h:
            return font, tw, th
    font = ImageFont.load_default(size=8)
    bbox = draw.textbbox((0, 0), text, font=font)
    return font, bbox[2] - bbox[0], bbox[3] - bbox[1]


def overlay_translations(image_bytes: bytes, regions: list[dict]) -> bytes:
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    w, h = img.size
    draw = ImageDraw.Draw(img)

    for region in regions:
        box = region["box"]
        text = region["translation"]

        y_min = max(0, int(box[0] * h / 1000))
        x_min = max(0, int(box[1] * w / 1000))
        y_max = min(h, int(box[2] * h / 1000))
        x_max = min(w, int(box[3] * w / 1000))

        if x_max <= x_min or y_max <= y_min:
            continue

        # Sample border pixels to estimate background
        border: list[tuple] = []
        for x in range(x_min, x_max):
            if y_min < h:
                border.append(img.getpixel((x, y_min)))
            if y_max - 1 < h:
                border.append(img.getpixel((x, y_max - 1)))
        for y in range(y_min, y_max):
            if x_min < w:
                border.append(img.getpixel((x_min, y)))
            if x_max - 1 < w:
                border.append(img.getpixel((x_max - 1, y)))

        bg = _median_color(border)
        fg = _contrasting(bg)

        draw.rectangle([x_min, y_min, x_max, y_max], fill=bg)

        box_w = x_max - x_min - 4
        box_h = y_max - y_min - 4
        font, tw, th = _fit_text(draw, text, box_w, box_h)

        tx = x_min + (box_w - tw) // 2 + 2
        ty = y_min + (box_h - th) // 2 + 2
        draw.text((tx, ty), text, fill=fg, font=font)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()
