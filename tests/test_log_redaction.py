"""Tokens must not reach bot.log: httpx logs every request URL, and ours carry secrets."""

import logging

from src.log_redaction import RedactSecrets, install_secret_redaction


def _rendered(message: str, *args) -> str:
    record = logging.LogRecord("httpx", logging.INFO, __file__, 1, message, args, None)
    RedactSecrets().filter(record)
    return record.getMessage()


def test_telegram_token_is_redacted():
    url = "HTTP Request: POST https://api.telegram.org/bot8353275794:AAHSgko5ItnDpZI0OMQ/sendPhoto"
    assert _rendered(url) == "HTTP Request: POST https://api.telegram.org/bot8353275794:<redacted>/sendPhoto"


def test_api_key_query_parameter_is_redacted():
    url = "POST https://generativelanguage.googleapis.com/v1beta/models/x:generateContent?key=AIzaSyCKN-TWek"
    assert _rendered(url).endswith("generateContent?key=<redacted>")


def test_admin_secret_is_redacted():
    assert _rendered("GET /admin/prompt?secret=hunter2 HTTP/1.1") == "GET /admin/prompt?secret=<redacted> HTTP/1.1"


def test_secret_passed_as_an_argument_is_redacted():
    """httpx builds its line from %s arguments, so the secret is often not in the template."""
    assert _rendered("HTTP Request: %s", "GET https://x/?key=abc123") == "HTTP Request: GET https://x/?key=<redacted>"


def test_other_query_parameters_survive():
    line = "GET https://www.reddit.com/r/all/top/.rss?t=day&limit=50"
    assert _rendered(line) == line


def test_ordinary_message_is_untouched():
    assert _rendered("Scrape done: found=%d new=%d", 50, 7) == "Scrape done: found=50 new=7"


def test_install_adds_the_filter_to_root_handlers():
    root = logging.getLogger()
    handler = logging.NullHandler()
    root.addHandler(handler)
    try:
        install_secret_redaction()
        assert any(isinstance(f, RedactSecrets) for f in handler.filters)
    finally:
        root.removeHandler(handler)
