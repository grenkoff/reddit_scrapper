"""The explanation stream must survive a model going offline, like the translations do."""

import json

import respx
from httpx import Response

from src.config import Config
from src.explainer.gemini import stream_explanation

CONFIG = Config(
    telegram_bot_token="t",
    telegram_chat_id="t",
    database_url="postgresql://test",
    gemini_api_key="key",
)
POST = {"reddit_id": "t3_abc", "title": "A post", "subreddit": "pics", "post_type": "image", "score": 1}
OVERLOADED = Response(503, json={"error": {"message": "This model is currently experiencing high demand."}})


def _stream_url(model: str) -> str:
    return f"https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent"


def _sse(*chunks: str) -> Response:
    body = "".join(
        "data: " + json.dumps({"candidates": [{"content": {"parts": [{"text": c}]}}]}) + "\n\n" for c in chunks
    )
    return Response(200, content=body.encode(), headers={"Content-Type": "text/event-stream"})


@respx.mock
async def test_stream_falls_back_to_an_available_model(monkeypatch):
    async def no_comments(*_a, **_k):
        return []

    monkeypatch.setattr("src.explainer.gemini._safe_fetch_comments", no_comments)
    first = respx.post(_stream_url("gemini-3.1-flash-lite")).mock(return_value=OVERLOADED)
    second = respx.post(_stream_url("gemini-3.5-flash")).mock(return_value=_sse("Объяс", "нение"))

    chunks = [c async for c in stream_explanation(CONFIG, POST)]

    assert "".join(chunks) == "Объяснение"
    assert first.called and second.called
