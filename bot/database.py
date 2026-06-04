"""
Asya Bot Database — SQLite with aiosqlite
Tables: users, chat_history, news_items, channel_posts, ai_cache, partner_posts
"""

import aiosqlite
import json
import time
from typing import Optional, List, Dict, Any

from bot.config import config


DB_PATH = config.DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT DEFAULT '',
    first_name TEXT DEFAULT '',
    last_name TEXT DEFAULT '',
    language_code TEXT DEFAULT 'ru',
    is_blocked INTEGER DEFAULT 0,
    is_admin INTEGER DEFAULT 0,
    first_seen REAL DEFAULT 0,
    last_seen REAL DEFAULT 0,
    message_count INTEGER DEFAULT 0,
    chat_mode TEXT DEFAULT 'normal'
);

CREATE TABLE IF NOT EXISTS chat_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    timestamp REAL DEFAULT 0,
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

CREATE TABLE IF NOT EXISTS news_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT UNIQUE NOT NULL,
    summary TEXT DEFAULT '',
    published REAL DEFAULT 0,
    fetched_at REAL DEFAULT 0,
    is_posted INTEGER DEFAULT 0,
    category TEXT DEFAULT 'auto',
    lang TEXT DEFAULT 'ru'
);

CREATE TABLE IF NOT EXISTS channel_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER DEFAULT 0,
    content TEXT NOT NULL,
    post_type TEXT DEFAULT 'news',
    source_url TEXT DEFAULT '',
    created_at REAL DEFAULT 0,
    partner_program TEXT DEFAULT '',
    views INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS ai_cache (
    query_hash TEXT PRIMARY KEY,
    query TEXT NOT NULL,
    response TEXT NOT NULL,
    model TEXT DEFAULT '',
    created_at REAL DEFAULT 0,
    hit_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS partner_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    program_id TEXT NOT NULL,
    program_name TEXT NOT NULL,
    category TEXT NOT NULL,
    affiliate_url TEXT NOT NULL,
    post_content TEXT DEFAULT '',
    posted_at REAL DEFAULT 0,
    message_id INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_chat_history_user ON chat_history(user_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_news_items_url ON news_items(url);
CREATE INDEX IF NOT EXISTS idx_news_items_posted ON news_items(is_posted, published);
CREATE INDEX IF NOT EXISTS idx_channel_posts_created ON channel_posts(created_at);
CREATE INDEX IF NOT EXISTS idx_ai_cache_query ON ai_cache(query_hash);
CREATE INDEX IF NOT EXISTS idx_partner_posts_category ON partner_posts(category, posted_at);
"""


async def init_db() -> None:
    """Initialize database — create all tables."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()


async def get_or_create_user(user_id: int, username: str = "", first_name: str = "",
                              last_name: str = "", language_code: str = "ru") -> Dict[str, Any]:
    """Get or create a user record. Returns user dict."""
    now = time.time()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()

        if row is None:
            await db.execute(
                """INSERT INTO users (user_id, username, first_name, last_name, language_code,
                   first_seen, last_seen, message_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 1)""",
                (user_id, username, first_name, last_name, language_code, now, now),
            )
            await db.commit()
            return {
                "user_id": user_id, "username": username, "first_name": first_name,
                "last_name": last_name, "language_code": language_code,
                "is_blocked": 0, "is_admin": 0, "first_seen": now,
                "last_seen": now, "message_count": 1, "chat_mode": "normal",
            }
        else:
            await db.execute(
                """UPDATE users SET username=?, first_name=?, last_name=?,
                   last_seen=?, message_count=message_count+1 WHERE user_id=?""",
                (username, first_name, last_name, now, user_id),
            )
            await db.commit()
            return dict(row)


async def is_user_blocked(user_id: int) -> bool:
    """Check if a user is blocked."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT is_blocked FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            return bool(row and row[0])


async def set_user_admin(user_id: int, is_admin: bool = True) -> None:
    """Set or unset a user as admin."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET is_admin = ? WHERE user_id = ?", (int(is_admin), user_id))
        await db.commit()


async def is_user_admin(user_id: int) -> bool:
    """Check if a user is admin (or is the owner)."""
    if user_id == config.OWNER_ID:
        return True
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT is_admin FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            return bool(row and row[0])


async def block_user(user_id: int, blocked: bool = True) -> None:
    """Block or unblock a user."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET is_blocked = ? WHERE user_id = ?", (int(blocked), user_id))
        await db.commit()


async def set_chat_mode(user_id: int, mode: str) -> None:
    """Set chat mode for a user (normal, diagnostic, parts)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET chat_mode = ? WHERE user_id = ?", (mode, user_id))
        await db.commit()


async def get_chat_mode(user_id: int) -> str:
    """Get current chat mode for a user."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT chat_mode FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else "normal"


async def add_chat_message(user_id: int, role: str, content: str) -> None:
    """Add a message to chat history."""
    now = time.time()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO chat_history (user_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            (user_id, role, content, now),
        )
        await db.commit()
        # Prune old messages to keep history manageable
        await _prune_chat_history(db, user_id)


async def _prune_chat_history(db: aiosqlite.Connection, user_id: int) -> None:
    """Keep only the most recent N messages for a user."""
    limit = config.CHAT_HISTORY_LIMIT
    async with db.execute(
        """DELETE FROM chat_history WHERE user_id = ? AND id NOT IN (
           SELECT id FROM chat_history WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?
        )""",
        (user_id, user_id, limit),
    ) as cursor:
        pass
    await db.commit()


async def get_chat_history(user_id: int, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Get chat history for a user, most recent first."""
    limit = limit or config.CHAT_HISTORY_LIMIT
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT role, content, timestamp FROM chat_history WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?",
            (user_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in reversed(rows)]


async def clear_chat_history(user_id: int) -> None:
    """Clear chat history for a user."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM chat_history WHERE user_id = ?", (user_id,))
        await db.commit()


async def add_news_item(source: str, title: str, url: str, summary: str = "",
                         published: float = 0, category: str = "auto", lang: str = "ru") -> bool:
    """Add a news item. Returns True if new, False if duplicate."""
    now = time.time()
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """INSERT INTO news_items (source, title, url, summary, published, fetched_at, category, lang)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (source, title, url, summary, published, now, category, lang),
            )
            await db.commit()
            return True
    except aiosqlite.IntegrityError:
        return False


async def get_unposted_news(limit: int = 10, category: str = "") -> List[Dict[str, Any]]:
    """Get unposted news items, ordered by publish date."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if category:
            async with db.execute(
                """SELECT * FROM news_items WHERE is_posted = 0 AND category = ?
                   ORDER BY published DESC LIMIT ?""",
                (category, limit),
            ) as cursor:
                rows = await cursor.fetchall()
        else:
            async with db.execute(
                """SELECT * FROM news_items WHERE is_posted = 0
                   ORDER BY published DESC LIMIT ?""",
                (limit,),
            ) as cursor:
                rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def mark_news_posted(url: str) -> None:
    """Mark a news item as posted to channel."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE news_items SET is_posted = 1 WHERE url = ?", (url,))
        await db.commit()


async def add_channel_post(content: str, message_id: int = 0, post_type: str = "news",
                            source_url: str = "", partner_program: str = "") -> int:
    """Add a channel post record. Returns post ID."""
    now = time.time()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO channel_posts (message_id, content, post_type, source_url, created_at, partner_program)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (message_id, content, post_type, source_url, now, partner_program),
        )
        await db.commit()
        return cursor.lastrowid


async def get_today_post_count() -> int:
    """Get number of posts made today."""
    today_start = time.time() - (time.time() % 86400)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM channel_posts WHERE created_at >= ?", (today_start,),
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0


async def get_ai_cached(query_hash: str) -> Optional[str]:
    """Get cached AI response if available and fresh (< 1 hour)."""
    max_age = time.time() - 3600
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT response FROM ai_cache WHERE query_hash = ? AND created_at > ?",
            (query_hash, max_age),
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                await db.execute(
                    "UPDATE ai_cache SET hit_count = hit_count + 1 WHERE query_hash = ?",
                    (query_hash,),
                )
                await db.commit()
                return row[0]
            return None


async def set_ai_cached(query_hash: str, query: str, response: str, model: str = "") -> None:
    """Cache an AI response."""
    now = time.time()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT OR REPLACE INTO ai_cache (query_hash, query, response, model, created_at, hit_count)
               VALUES (?, ?, ?, ?, ?, 0)""",
            (query_hash, query, response, model, now),
        )
        await db.commit()


async def add_partner_post(program_id: str, program_name: str, category: str,
                            affiliate_url: str, post_content: str = "",
                            message_id: int = 0) -> int:
    """Add a partner post record."""
    now = time.time()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO partner_posts (program_id, program_name, category, affiliate_url,
               post_content, posted_at, message_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (program_id, program_name, category, affiliate_url, post_content, now, message_id),
        )
        await db.commit()
        return cursor.lastrowid


async def get_today_partner_post_count() -> int:
    """Get number of partner posts made today."""
    today_start = time.time() - (time.time() % 86400)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM partner_posts WHERE posted_at >= ?", (today_start,),
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0


async def get_stats() -> Dict[str, Any]:
    """Get bot statistics."""
    async with aiosqlite.connect(DB_PATH) as db:
        stats = {}
        async with db.execute("SELECT COUNT(*) FROM users") as cursor:
            stats["total_users"] = (await cursor.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM users WHERE is_blocked = 0") as cursor:
            stats["active_users"] = (await cursor.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM news_items") as cursor:
            stats["total_news"] = (await cursor.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM news_items WHERE is_posted = 0") as cursor:
            stats["unposted_news"] = (await cursor.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM channel_posts") as cursor:
            stats["total_posts"] = (await cursor.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM partner_posts") as cursor:
            stats["partner_posts"] = (await cursor.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM ai_cache") as cursor:
            stats["cached_queries"] = (await cursor.fetchone())[0]
        return stats
