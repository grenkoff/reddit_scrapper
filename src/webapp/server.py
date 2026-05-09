import asyncio
import logging

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from src.config import Config
from src.db import get_explanation, get_post, save_explanation
from src.explainer.gemini import generate_explanation

logger = logging.getLogger(__name__)


_MINI_APP_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AI-объяснение</title>
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
  .error { color: var(--tg-theme-destructive-text-color, #e74c3c); padding: 16px 0; }
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
  <div id="content" class="explanation" style="display:none;"></div>
  <div id="error" class="error" style="display:none;"></div>
</div>
<script>
  const tg = window.Telegram?.WebApp;
  if (tg) { tg.ready(); tg.expand(); }

  const params = new URLSearchParams(window.location.search);
  const startParam = tg?.initDataUnsafe?.start_param || params.get('reddit_id');

  function showError(msg) {
    document.getElementById('loading').style.display = 'none';
    const err = document.getElementById('error');
    err.textContent = msg;
    err.style.display = 'block';
  }

  if (!startParam) {
    showError('Параметр поста не указан.');
  } else {
    fetch('/api/explain?reddit_id=' + encodeURIComponent(startParam))
      .then(r => r.json())
      .then(data => {
        document.getElementById('loading').style.display = 'none';
        if (data.error) {
          showError(data.error);
        } else {
          const c = document.getElementById('content');
          c.textContent = data.explanation;
          c.style.display = 'block';
        }
      })
      .catch(e => showError('Ошибка сети: ' + e));
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

    @app.get("/api/explain")
    async def explain(reddit_id: str) -> JSONResponse:
        cached = await get_explanation(reddit_id)
        if cached:
            return JSONResponse({"explanation": cached, "cached": True})

        if not config.gemini_api_key:
            return JSONResponse({"error": "AI не настроен."})

        post = await get_post(reddit_id)
        if not post:
            return JSONResponse({"error": "Пост не найден."})

        try:
            explanation = await generate_explanation(config, post)
        except Exception as e:
            logger.warning("Gemini error for %s: %s", reddit_id, e)
            return JSONResponse({"error": "Не удалось сгенерировать объяснение."})

        await save_explanation(reddit_id, explanation)
        return JSONResponse({"explanation": explanation, "cached": False})

    return app


async def run_webapp(config: Config) -> None:
    app = create_app(config)
    server_config = uvicorn.Config(app, host="0.0.0.0", port=config.webapp_port, log_level="info")
    server = uvicorn.Server(server_config)
    await server.serve()


def start_webapp_task(config: Config) -> asyncio.Task:
    return asyncio.create_task(run_webapp(config))
