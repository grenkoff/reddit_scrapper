import pytest

import src.scraper.reddit as reddit


@pytest.fixture(autouse=True)
def no_reddit_pacing(monkeypatch):
    """Drop the scraper's 60s inter-request pacing so tests don't wait it out."""
    monkeypatch.setattr(reddit, "_MIN_REQUEST_INTERVAL", 0.0)
    monkeypatch.setattr(reddit, "_last_request_at", 0.0)
