import pytest

import src.scraper.reddit as reddit


@pytest.fixture(autouse=True)
def no_reddit_pacing(monkeypatch):
    """Drop the scraper's inter-request pacing so tests don't wait it out."""
    for pacer in (reddit._FEED_PACER, reddit._EMBED_PACER):
        monkeypatch.setattr(pacer, "interval", 0.0)
        monkeypatch.setattr(pacer, "last_at", 0.0)
