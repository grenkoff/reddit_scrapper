import os
from dataclasses import dataclass


@dataclass
class Config:
    telegram_bot_token: str
    telegram_chat_id: str
    reddit_user_agent: str = "reddit-scrapper/0.1 (by u/your_username)"
    scrape_interval: int = 1200  # seconds (20 minutes)
    posts_limit: int = 50
    skip_nsfw: bool = True
    pause_between_posts: float = 3.0  # seconds


def load_config() -> Config:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN is not set")
    if not chat_id:
        raise ValueError("TELEGRAM_CHAT_ID is not set")

    return Config(
        telegram_bot_token=token,
        telegram_chat_id=chat_id,
        reddit_user_agent=os.getenv("REDDIT_USER_AGENT", "reddit-scrapper/0.1 (by u/your_username)"),
        scrape_interval=int(os.getenv("SCRAPE_INTERVAL", "1200")),
        posts_limit=int(os.getenv("POSTS_LIMIT", "50")),
        skip_nsfw=os.getenv("SKIP_NSFW", "true").lower() == "true",
        pause_between_posts=float(os.getenv("PAUSE_BETWEEN_POSTS", "3.0")),
    )
