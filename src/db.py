import json
import logging
from datetime import UTC, datetime

import asyncpg

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None


async def init_db(database_url: str) -> None:
    global _pool
    _pool = await asyncpg.create_pool(database_url, min_size=1, max_size=5)
    async with _pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS posts (
                id              SERIAL PRIMARY KEY,
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
                is_nsfw         BOOLEAN NOT NULL DEFAULT FALSE,
                media_urls      TEXT,
                created_utc     TIMESTAMPTZ NOT NULL,
                scraped_at      TIMESTAMPTZ NOT NULL,
                published_to_tg BOOLEAN NOT NULL DEFAULT FALSE,
                published_at    TIMESTAMPTZ,
                tg_message_id   INTEGER,
                preview_url     TEXT,
                video_url       TEXT,
                hls_url         TEXT
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS scrape_logs (
                id              SERIAL PRIMARY KEY,
                started_at      TIMESTAMPTZ NOT NULL,
                finished_at     TIMESTAMPTZ,
                posts_found     INTEGER NOT NULL DEFAULT 0,
                posts_new       INTEGER NOT NULL DEFAULT 0,
                posts_published INTEGER NOT NULL DEFAULT 0,
                error           TEXT
            )
        """)
    logger.info("Database initialized")


async def close_db() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


async def is_post_exists(reddit_id: str) -> bool:
    async with _pool.acquire() as conn:
        row = await conn.fetchrow("SELECT 1 FROM posts WHERE reddit_id = $1", reddit_id)
        return row is not None


async def insert_post(post: dict) -> None:
    async with _pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO posts
                (reddit_id, subreddit, title, author, url, content_url, selftext,
                 score, num_comments, post_type, is_nsfw, media_urls,
                 created_utc, scraped_at, preview_url, video_url, hls_url)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17)
            ON CONFLICT (reddit_id) DO NOTHING
            """,
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
        )


async def get_unpublished_posts(limit: int | None = None) -> list[dict]:
    query = "SELECT * FROM posts WHERE published_to_tg = FALSE ORDER BY score DESC"
    if limit:
        query += f" LIMIT {limit}"
    async with _pool.acquire() as conn:
        rows = await conn.fetch(query)
    posts = [dict(row) for row in rows]
    for post in posts:
        if post["media_urls"]:
            post["media_urls"] = json.loads(post["media_urls"])
    return posts


async def mark_as_published(reddit_id: str, tg_message_id: int) -> None:
    async with _pool.acquire() as conn:
        await conn.execute(
            "UPDATE posts SET published_to_tg = TRUE, published_at = $1, tg_message_id = $2 WHERE reddit_id = $3",
            datetime.now(UTC).isoformat(),
            tg_message_id,
            reddit_id,
        )


async def log_scrape(
    started_at: datetime,
    finished_at: datetime,
    posts_found: int,
    posts_new: int,
    posts_published: int,
    error: str | None = None,
) -> None:
    async with _pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO scrape_logs (started_at, finished_at, posts_found, posts_new, posts_published, error)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            started_at.isoformat(),
            finished_at.isoformat(),
            posts_found,
            posts_new,
            posts_published,
            error,
        )
