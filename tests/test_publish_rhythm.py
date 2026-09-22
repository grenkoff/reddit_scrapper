"""A restart must resume the publishing rhythm instead of firing a post straight away."""

import math
import time
from datetime import UTC, datetime, timedelta

import pytest

import src.main as main
from src.config import Config

CONFIG = Config(
    telegram_bot_token="t",
    telegram_chat_id="t",
    database_url="postgresql://test",
    pause_between_posts=900.0,
)


def _seconds_until_due(seeded: float) -> float:
    """What the loop computes as the wait before its first publish."""
    return CONFIG.pause_between_posts - (time.monotonic() - seeded)


async def test_recent_post_delays_the_first_publish(monkeypatch):
    async def fake_last_published():
        return datetime.now(UTC) - timedelta(seconds=60)

    monkeypatch.setattr(main, "get_last_published_at", fake_last_published)

    seeded = await main._last_publish_monotonic(CONFIG)

    # 900s interval minus the 60s already elapsed, give or take the clock read itself
    assert _seconds_until_due(seeded) == pytest.approx(840, abs=1)


async def test_post_older_than_the_interval_publishes_now(monkeypatch):
    async def fake_last_published():
        return datetime.now(UTC) - timedelta(seconds=CONFIG.pause_between_posts + 1)

    monkeypatch.setattr(main, "get_last_published_at", fake_last_published)

    assert await main._last_publish_monotonic(CONFIG) == -math.inf


async def test_empty_channel_publishes_now(monkeypatch):
    async def fake_last_published():
        return None

    monkeypatch.setattr(main, "get_last_published_at", fake_last_published)

    assert await main._last_publish_monotonic(CONFIG) == -math.inf


async def test_unreadable_history_publishes_now(monkeypatch):
    """A database hiccup must not stall publishing — fall back to the old behaviour."""

    async def fake_last_published():
        raise RuntimeError("connection reset")

    monkeypatch.setattr(main, "get_last_published_at", fake_last_published)

    assert await main._last_publish_monotonic(CONFIG) == -math.inf
