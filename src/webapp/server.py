import asyncio
import json
import logging

import httpx
import uvicorn
from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from src.config import Config
from src.db import (
    get_explanation,
    get_post,
    get_setting,
    get_translated_image,
    mark_as_unpublished,
    save_explanation,
    save_setting,
    save_translated_image,
)
from src.explainer.gemini import _SYSTEM_PROMPT_DEFAULT, stream_explanation
from src.explainer.image_processor import detect_image_text, overlay_translations

logger = logging.getLogger(__name__)


_MINI_APP_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AI-explanation</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  body {
    margin: 0;
    padding: 16px;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: var(--tg-theme-bg-color, #ffffff);
    color: var(--tg-theme-text-color, #000000);
    line-height: 1.5;
    font-size: 15px;
  }
  .container { max-width: 600px; margin: 0 auto; }
  .shimmer {
    background: linear-gradient(90deg,
      var(--tg-theme-secondary-bg-color, #f0f0f0) 0%,
      var(--tg-theme-hint-color, #cccccc) 50%,
      var(--tg-theme-secondary-bg-color, #f0f0f0) 100%);
    background-size: 200% 100%;
    animation: shimmer 1.4s infinite;
    border-radius: 6px;
    height: 14px;
    margin-bottom: 10px;
    opacity: 0.5;
  }
  .shimmer.short { width: 70%; }
  @keyframes shimmer {
    0% { background-position: 200% 0; }
    100% { background-position: -200% 0; }
  }
  .explanation { white-space: pre-wrap; }
  .explanation blockquote {
    margin: 4px 0;
    padding-left: 10px;
    border-left: 3px solid var(--tg-theme-hint-color, #cccccc);
    color: var(--tg-theme-hint-color, #707579);
    display: inline-block;
  }
  .explanation strong { font-weight: 600; }
  .cursor::after {
    content: '▍';
    animation: blink 1s infinite;
    opacity: 0.5;
    margin-left: 2px;
  }
  @keyframes blink {
    50% { opacity: 0; }
  }
  .error { color: var(--tg-theme-destructive-text-color, #e74c3c); padding: 16px 0; }
  .post-image { width: 100%; border-radius: 8px; margin-bottom: 12px; display: block; }
</style>
</head>
<body>
<div class="container">
  <div id="loading">
    <div class="shimmer"></div>
    <div class="shimmer"></div>
    <div class="shimmer"></div>
    <div class="shimmer short"></div>
  </div>
  <div id="content" class="explanation cursor" style="display:none;"></div>
  <div id="error" class="error" style="display:none;"></div>
</div>
<script>
  const tg = window.Telegram?.WebApp;
  if (tg) { tg.ready(); tg.expand(); }

  const params = new URLSearchParams(window.location.search);
  const startParam = tg?.initDataUnsafe?.start_param || params.get('reddit_id');

  function showError(msg) {
    document.getElementById('loading').style.display = 'none';
    document.getElementById('content').style.display = 'none';
    const err = document.getElementById('error');
    err.textContent = msg;
    err.style.display = 'block';
  }

  function escapeHtml(s) {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  function renderMarkdown(text) {
    const lines = text.split('\\n');
    const out = lines.map((line) => {
      // Blockquote (line starts with `> `)
      let isQuote = false;
      if (line.startsWith('> ')) {
        isQuote = true;
        line = line.slice(2);
      } else if (line === '>') {
        isQuote = true;
        line = '';
      }
      let html = escapeHtml(line);
      // Bold: **text**
      html = html.replace(/\\*\\*([^*]+)\\*\\*/g, '<strong>$1</strong>');
      // Italic: *text*
      html = html.replace(/\\*([^*]+)\\*/g, '<em>$1</em>');
      return isQuote ? '<blockquote>' + html + '</blockquote>' : html;
    });
    return out.join('\\n');
  }

  function startStreaming(redditId) {
    const loading = document.getElementById('loading');
    const content = document.getElementById('content');
    const evt = new EventSource('/api/explain/stream?reddit_id=' + encodeURIComponent(redditId));
    let started = false;
    let buffer = '';

    evt.addEventListener('image', (e) => {
      const url = JSON.parse(e.data);
      const img = document.createElement('img');
      img.src = url;
      img.className = 'post-image';
      document.querySelector('.container').prepend(img);
      loading.style.display = 'none';
    });
    evt.addEventListener('chunk', (e) => {
      if (!started) {
        loading.style.display = 'none';
        content.style.display = 'block';
        started = true;
      }
      buffer += JSON.parse(e.data);
      content.innerHTML = renderMarkdown(buffer);
    });
    evt.addEventListener('done', () => {
      content.classList.remove('cursor');
      evt.close();
    });
    evt.addEventListener('error', (e) => {
      evt.close();
      if (!started) {
        const msg = e.data ? JSON.parse(e.data) : 'Не удалось сгенерировать объяснение.';
        showError(msg);
      } else {
        content.classList.remove('cursor');
      }
    });
  }

  if (!startParam) {
    showError('Параметр поста не указан.');
  } else {
    startStreaming(startParam);
  }
</script>
</body>
</html>
"""


def create_app(config: Config) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return _MINI_APP_HTML

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True}

    @app.post("/admin/reset")
    async def admin_reset(reddit_id: str, secret: str) -> JSONResponse:
        if secret != (config.reddit_proxy_secret or ""):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        await mark_as_unpublished(reddit_id)
        return JSONResponse({"ok": True, "reddit_id": reddit_id})

    @app.get("/admin/prompt", response_class=HTMLResponse)
    async def prompt_page(secret: str = "") -> HTMLResponse:
        if secret != (config.reddit_proxy_secret or ""):
            return HTMLResponse("<h3>Unauthorized</h3>", status_code=401)
        current = (await get_setting("system_prompt")) or _SYSTEM_PROMPT_DEFAULT
        escaped = current.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>System Prompt</title>
<style>
  body {{ font-family: sans-serif; max-width: 800px; margin: 40px auto; padding: 0 20px; background: #f5f5f5; }}
  h2 {{ color: #333; }}
  textarea {{ width: 100%; height: 500px; font-family: monospace; font-size: 14px; padding: 12px;
             border: 1px solid #ccc; border-radius: 6px; box-sizing: border-box; resize: vertical; }}
  button {{ margin-top: 12px; padding: 10px 24px; background: #2d7dd2; color: white;
            border: none; border-radius: 6px; font-size: 15px; cursor: pointer; }}
  button:hover {{ background: #245fa8; }}
  .saved {{ color: green; margin-left: 12px; display: none; }}
</style>
</head>
<body>
<h2>System Prompt</h2>
<form method="post" action="/admin/prompt?secret={secret}">
  <textarea name="prompt">{escaped}</textarea><br>
  <button type="submit">Сохранить</button>
  <span class="saved" id="saved">✓ Сохранено</span>
</form>
</body>
</html>"""
        return HTMLResponse(html)

    @app.post("/admin/prompt", response_class=HTMLResponse)
    async def prompt_save(secret: str = "", prompt: str = Form(default="")) -> HTMLResponse:
        if secret != (config.reddit_proxy_secret or ""):
            return HTMLResponse("<h3>Unauthorized</h3>", status_code=401)
        await save_setting("system_prompt", prompt.strip())
        logger.info("System prompt updated (%d chars)", len(prompt))
        return HTMLResponse(f'<meta http-equiv="refresh" content="0;url=/admin/prompt?secret={secret}">')

    @app.get("/api/image/{reddit_id}")
    async def image_endpoint(reddit_id: str):
        data = await get_translated_image(reddit_id)
        if not data:
            return Response(status_code=404)
        return Response(content=data, media_type="image/jpeg")

    @app.get("/api/explain/stream")
    async def explain_stream(reddit_id: str):
        def sse(event: str, data: str) -> str:
            return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

        async def event_stream():
            if not config.gemini_api_key:
                yield sse("error", "AI не настроен.")
                return

            cached = await get_explanation(reddit_id)
            post = await get_post(reddit_id)
            if not post:
                yield sse("error", "Пост не найден.")
                return

            # For non-image posts: serve from cache if available
            if cached and len(cached) >= 50 and post.get("post_type") != "image":
                yield sse("chunk", cached)
                yield sse("done", "")
                return

            # Try image text translation for single-image posts
            skip_image_text = False
            if post.get("post_type") == "image":
                try:
                    image_data = await get_translated_image(reddit_id)
                    if not image_data:
                        image_url = post.get("content_url") or post.get("preview_url")
                        if image_url:
                            regions = await detect_image_text(image_url, post, config)
                            if regions:
                                async with httpx.AsyncClient(timeout=15) as img_client:
                                    raw_resp = await img_client.get(image_url, follow_redirects=True)
                                raw_resp.raise_for_status()
                                image_data = overlay_translations(raw_resp.content, regions)
                                await save_translated_image(reddit_id, image_data)
                    if image_data:
                        yield sse("image", f"/api/image/{reddit_id}")
                        skip_image_text = True
                except Exception as e:
                    logger.debug("Image translation pipeline failed for %s: %s", reddit_id, e)

            # Serve from cache only when image pipeline is consistent with cache:
            # - no image overlay (pipeline didn't run) and cache exists → use cache
            # - image overlay generated but cache was made before (may contain section 4) → regenerate
            if cached and len(cached) >= 50 and not skip_image_text:
                yield sse("chunk", cached)
                yield sse("done", "")
                return

            full_text = ""
            try:
                async for chunk in stream_explanation(config, post, skip_image_text=skip_image_text):
                    full_text += chunk
                    yield sse("chunk", chunk)
            except Exception as e:
                logger.warning("Gemini stream error for %s: %s", reddit_id, e)
                yield sse("error", "Не удалось сгенерировать объяснение.")
                return

            text = full_text.strip()
            if text and text[-1] in ".!?*_»\"'）)":
                await save_explanation(reddit_id, text)
            yield sse("done", "")

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    @app.get("/api/explain")
    async def explain(reddit_id: str) -> JSONResponse:
        cached = await get_explanation(reddit_id)
        if cached:
            return JSONResponse({"explanation": cached, "cached": True})
        return JSONResponse({"error": "Use /api/explain/stream to generate"})

    return app


async def run_webapp(config: Config) -> None:
    app = create_app(config)
    server_config = uvicorn.Config(app, host="0.0.0.0", port=config.webapp_port, log_level="info")
    server = uvicorn.Server(server_config)
    await server.serve()


def start_webapp_task(config: Config) -> asyncio.Task:
    return asyncio.create_task(run_webapp(config))
