import contextlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

DB_PATH = Path("data/reddit_scrapper.db")


async def init_db() -> None:
    DB_PATH.parent.mkdir(exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS posts (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                reddit_id       TEXT UNIQUE NOT NULL,
                subreddit       TEXT NOT NULL,
                title           TEXT NOT NULL,
                author          TEXT NOT NULL,
                url             TEXT NOT NULL,
                content_url     TEXT,
                selftext        TEXT,
                score           INTEGER NOT NULL DEFAULT 0,
                num_comments    INTEGER NOT NULL DEFAULT 0,
                post_type       TEXT NOT NULL,
                is_nsfw         BOOLEAN NOT NULL DEFAULT 0,
                media_urls      TEXT,
                created_utc     DATETIME NOT NULL,
                scraped_at      DATETIME NOT NULL,
                published_to_tg BOOLEAN NOT NULL DEFAULT 0,
                published_at    DATETIME,
                tg_message_id   INTEGER
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS scrape_logs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at      DATETIME NOT NULL,
                finished_at     DATETIME,
                posts_found     INTEGER NOT NULL DEFAULT 0,
                posts_new       INTEGER NOT NULL DEFAULT 0,
                posts_published INTEGER NOT NULL DEFAULT 0,
                error           TEXT
            )
        """)
        await db.commit()
        with contextlib.suppress(Exception):
            await db.execute("ALTER TABLE posts ADD COLUMN preview_url TEXT")
            await db.commit()
        with contextlib.suppress(Exception):
            await db.execute("ALTER TABLE posts ADD COLUMN video_url TEXT")
            await db.commit()
        with contextlib.suppress(Exception):
            await db.execute("ALTER TABLE posts ADD COLUMN hls_url TEXT")
            await db.commit()
    logger.info("Database initialized")


async def is_post_exists(reddit_id: str) -> bool:
    query = "SELECT 1 FROM posts WHERE reddit_id = ?"
    async with aiosqlite.connect(DB_PATH) as db, db.execute(query, (reddit_id,)) as cursor:
        return await cursor.fetchone() is not None


async def insert_post(post: dict) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR IGNORE INTO posts
                (reddit_id, subreddit, title, author, url, content_url, selftext,
                 score, num_comments, post_type, is_nsfw, media_urls,
                 created_utc, scraped_at, preview_url, video_url, hls_url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                post["reddit_id"],
                post["subreddit"],
                post["title"],
                post["author"],
                post["url"],
                post.get("content_url"),
                post.get("selftext"),
                post["score"],
                post["num_comments"],
                post["post_type"],
                post["is_nsfw"],
                json.dumps(post["media_urls"]) if post.get("media_urls") else None,
                post["created_utc"],
                datetime.now(UTC).isoformat(),
                post.get("preview_url"),
                post.get("video_url"),
                post.get("hls_url"),
            ),
        )
        await db.commit()


async def get_unpublished_posts(limit: int | None = None) -> list[dict]:
    query = "SELECT * FROM posts WHERE published_to_tg = 0 ORDER BY score DESC"
    if limit:
        query += f" LIMIT {limit}"
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query) as cursor:
            rows = await cursor.fetchall()
    posts = [dict(row) for row in rows]
    for post in posts:
        if post["media_urls"]:
            post["media_urls"] = json.loads(post["media_urls"])
    return posts


async def mark_as_published(reddit_id: str, tg_message_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE posts SET published_to_tg = 1, published_at = ?, tg_message_id = ? WHERE reddit_id = ?",
            (datetime.now(UTC).isoformat(), tg_message_id, reddit_id),
        )
        await db.commit()


async def log_scrape(
    started_at: datetime,
    finished_at: datetime,
    posts_found: int,
    posts_new: int,
    posts_published: int,
    error: str | None = None,
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO scrape_logs (started_at, finished_at, posts_found, posts_new, posts_published, error)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (started_at.isoformat(), finished_at.isoformat(), posts_found, posts_new, posts_published, error),
        )
        await db.commit()
