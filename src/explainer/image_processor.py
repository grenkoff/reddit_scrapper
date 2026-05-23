import io
import json
import logging
from pathlib import Path

import cv2
import httpx
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from src.config import Config

logger = logging.getLogger(__name__)

_FONT_SEARCH_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
]


def _load_ttf(size: int) -> ImageFont.FreeTypeFont | None:
    for path in _FONT_SEARCH_PATHS:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size=size)
            except Exception:
                continue
    return None


def _make_font(size: int) -> ImageFont.ImageFont:
    return _load_ttf(size) or ImageFont.load_default(size=size)


def _build_detect_prompt(post: dict) -> str:
    context_lines = [
        f'This is a Reddit post from r/{post["subreddit"]} titled: "{post["title"]}".',
    ]
    if post.get("selftext"):
        context_lines.append(f'Post body: "{post["selftext"][:500]}"')
    context = " ".join(context_lines)

    return (
        f"{context}\n\n"
        "Detect all text visible in this image. Translate each piece to Russian, "
        "using the post context above to make the translation natural and on-topic. "
        "Return ONLY valid JSON with no markdown, no explanation, no code block:\n"
        '[{"text": "original text", "translation": "русский перевод", '
        '"box": [y_min, x_min, y_max, x_max]}]\n'
        "box values are integers normalized to 0–1000 "
        "(y_min/y_max = top/bottom distance from top, x_min/x_max = left/right distance from left). "
        "If there is no text in the image return exactly: []"
    )


async def detect_image_text(image_url: str, post: dict, config: Config) -> list[dict]:
    from src.explainer.gemini import _fetch_image

    image_part = _fetch_image(image_url)
    if not image_part:
        return []

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [image_part, {"text": _build_detect_prompt(post)}],
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
        isinstance(r, dict) and "translation" in r and "box" in r and isinstance(r["box"], list) and len(r["box"]) == 4
    )


def _inpaint_regions(img_bgr: np.ndarray, regions_px: list[tuple[int, int, int, int]]) -> np.ndarray:
    """Use cv2.inpaint to reconstruct background where original text was."""
    h, w = img_bgr.shape[:2]
    mask = np.zeros((h, w), dtype=np.uint8)
    for x_min, y_min, x_max, y_max in regions_px:
        # Slightly expand the mask so inpaint covers anti-aliased text edges
        pad = max(2, (y_max - y_min) // 10)
        x0 = max(0, x_min - pad)
        y0 = max(0, y_min - pad)
        x1 = min(w, x_max + pad)
        y1 = min(h, y_max + pad)
        mask[y0:y1, x0:x1] = 255
    return cv2.inpaint(img_bgr, mask, inpaintRadius=5, flags=cv2.INPAINT_TELEA)


def _wrap_text(text: str, font: ImageFont.ImageFont, draw: ImageDraw.ImageDraw, max_width: int) -> list[str]:
    """Greedy word wrap so each line fits within max_width."""
    words = text.split()
    if not words:
        return []
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = current + " " + word
        bbox = draw.textbbox((0, 0), candidate, font=font)
        if bbox[2] - bbox[0] <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _fit_text(
    draw: ImageDraw.ImageDraw, text: str, box_w: int, box_h: int
) -> tuple[ImageFont.ImageFont, list[str], int]:
    """Find largest font size where text wraps to fit box_w × box_h."""
    for size in range(max(10, int(box_h * 0.9)), 9, -1):
        font = _make_font(size)
        lines = _wrap_text(text, font, draw, box_w)
        if not lines:
            continue
        line_h = draw.textbbox((0, 0), "Ay", font=font)[3]
        total_h = line_h * len(lines)
        max_line_w = max(draw.textbbox((0, 0), line, font=font)[2] for line in lines)
        if total_h <= box_h and max_line_w <= box_w:
            return font, lines, line_h
    font = _make_font(10)
    return font, [text], draw.textbbox((0, 0), "Ay", font=font)[3]


def _sample_text_color(img_bgr: np.ndarray, x_min: int, y_min: int, x_max: int, y_max: int) -> tuple[int, int, int]:
    """Estimate original text color: pick darkest or lightest cluster inside the box."""
    region = img_bgr[y_min:y_max, x_min:x_max]
    if region.size == 0:
        return (255, 255, 255)
    pixels = region.reshape(-1, 3)
    luminance = 0.114 * pixels[:, 0] + 0.587 * pixels[:, 1] + 0.299 * pixels[:, 2]
    # If many bright pixels → text is likely white; otherwise black
    bright_ratio = float(np.mean(luminance > 200))
    if bright_ratio > 0.15:
        return (255, 255, 255)
    return (0, 0, 0)


def overlay_translations(image_bytes: bytes, regions: list[dict]) -> bytes:
    pil_img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    w, h = pil_img.size

    # Convert regions to pixel coords and filter invalid
    regions_px: list[tuple[int, int, int, int]] = []
    valid_regions: list[tuple[dict, tuple[int, int, int, int]]] = []
    for region in regions:
        box = region["box"]
        y_min = max(0, int(box[0] * h / 1000))
        x_min = max(0, int(box[1] * w / 1000))
        y_max = min(h, int(box[2] * h / 1000))
        x_max = min(w, int(box[3] * w / 1000))
        if x_max <= x_min or y_max <= y_min:
            continue
        regions_px.append((x_min, y_min, x_max, y_max))
        valid_regions.append((region, (x_min, y_min, x_max, y_max)))

    if not valid_regions:
        return image_bytes

    # Inpaint backgrounds (cv2 uses BGR)
    img_bgr = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
    inpainted_bgr = _inpaint_regions(img_bgr, regions_px)
    out_img = Image.fromarray(cv2.cvtColor(inpainted_bgr, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(out_img)

    for region, (x_min, y_min, x_max, y_max) in valid_regions:
        text = region["translation"]
        box_w = x_max - x_min - 4
        box_h = y_max - y_min - 4
        font, lines, line_h = _fit_text(draw, text, box_w, box_h)

        text_color = _sample_text_color(img_bgr, x_min, y_min, x_max, y_max)
        outline_color = (0, 0, 0) if text_color == (255, 255, 255) else (255, 255, 255)
        outline_w = max(1, line_h // 12)

        total_h = line_h * len(lines)
        y = y_min + (box_h - total_h) // 2 + 2
        for line in lines:
            line_bbox = draw.textbbox((0, 0), line, font=font)
            line_w = line_bbox[2] - line_bbox[0]
            x = x_min + (box_w - line_w) // 2 + 2
            # Stroke (outline) for contrast on any background
            draw.text((x, y), line, fill=text_color, font=font, stroke_width=outline_w, stroke_fill=outline_color)
            y += line_h

    buf = io.BytesIO()
    out_img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()
