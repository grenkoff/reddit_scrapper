# Reddit Scraper → Telegram Bot

Automatically scrapes top posts from Reddit and publishes them to a Telegram channel with full media support (images, videos, galleries, text).

[Читать на русском](README.ru.md)

## Features

- Fetches top posts from Reddit every 20 minutes via public JSON API
- Supports all media types: images, galleries, videos, text posts, links
- Deduplication — same post is never published twice
- NSFW filtering
- Automatic retry on network errors
- Runs in Docker, auto-deploys on push to `main`

## Quick Start

### Docker (recommended)

```bash
git clone https://github.com/your_username/reddit_scrapper.git
cd reddit_scrapper
cp .env.example .env
# Edit .env with your tokens
docker compose up -d
```

### Manual

```bash
git clone https://github.com/your_username/reddit_scrapper.git
cd reddit_scrapper
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
# Edit .env with your tokens
python -m src.main
```

## Configuration

Copy `.env.example` to `.env` and fill in the values:

| Variable | Description | Default |
|----------|-------------|---------|
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather | required |
| `TELEGRAM_CHAT_ID` | Channel ID (e.g. `-1001234567890`) | required |
| `REDDIT_USER_AGENT` | User-Agent for Reddit requests | `reddit-scrapper/0.1` |
| `SCRAPE_INTERVAL` | Seconds between scrapes | `1200` (20 min) |
| `POSTS_LIMIT` | Max posts per request | `50` |
| `SKIP_NSFW` | Skip NSFW posts | `true` |
| `PAUSE_BETWEEN_POSTS` | Seconds between Telegram messages | `3.0` |

## Development

```bash
pip install -e ".[dev]"

# Lint
ruff check .
ruff format .

# Tests
pytest
```

### Branching

- `main` — production, auto-deploys on merge
- `feature/*` — feature branches, merged into `main` via PR
- `fix/*` — bug fixes, merged into `main` via PR

## License

[MIT](LICENSE) © 2026 Alexander Grenkov
