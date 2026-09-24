import logging
import re

# httpx logs every request at INFO, and our secrets live inside those URLs: the Telegram token is
# part of the path (api.telegram.org/bot<token>/...), while Gemini and the web app's admin pages
# pass theirs as a query parameter. Without this the tokens end up in bot.log and docker logs.
_SECRET_PATTERNS = (
    re.compile(r"(bot\d+:)[A-Za-z0-9_-]+"),
    re.compile(r"((?:key|secret|token|api_key)=)[^&\s\"']+", re.IGNORECASE),
)
_REPLACEMENT = r"\1<redacted>"


class RedactSecrets(logging.Filter):
    """Strip tokens and API keys out of log records before a handler writes them."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = message
        for pattern in _SECRET_PATTERNS:
            redacted = pattern.sub(_REPLACEMENT, redacted)
        if redacted != message:
            # Replace the whole formatted message: a secret can sit in the template or in any
            # argument, and substituting it back into %s placeholders would reinstate it.
            record.msg = redacted
            record.args = ()
        return True


def install_secret_redaction() -> None:
    """Attach the filter to every handler that is already configured.

    Filters live on handlers rather than loggers because a logger's filters do not apply to
    records propagated from its children, and httpx logs through its own logger.
    """
    redactor = RedactSecrets()
    for handler in logging.getLogger().handlers:
        handler.addFilter(redactor)
