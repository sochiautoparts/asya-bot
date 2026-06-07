"""
Asya Bot Database — SQLite with aiosqlite
Tables: users, chat_history, news_items, channel_posts, ai_cache, partner_posts
"""

import aiosqlite
import hashlib
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
    lang TEXT DEFAULT 'ru',
    image_urls TEXT DEFAULT '[]'
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

CREATE TABLE IF NOT EXISTS user_cars (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    brand TEXT DEFAULT '',
    model TEXT DEFAULT '',
    year INTEGER DEFAULT 0,
    vin TEXT DEFAULT '',
    engine TEXT DEFAULT '',
    mileage INTEGER DEFAULT 0,
    notes TEXT DEFAULT '',
    created_at REAL DEFAULT 0,
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

CREATE INDEX IF NOT EXISTS idx_user_cars_user ON user_cars(user_id);

CREATE TABLE IF NOT EXISTS post_fingerprints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title_hash TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    title_prefix TEXT NOT NULL,
    post_id INTEGER DEFAULT 0,
    created_at REAL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_fingerprints_title ON post_fingerprints(title_hash);
CREATE INDEX IF NOT EXISTS idx_fingerprints_content ON post_fingerprints(content_hash);
CREATE INDEX IF NOT EXISTS idx_fingerprints_created ON post_fingerprints(created_at);

CREATE TABLE IF NOT EXISTS topic_registry (
    entity_key TEXT PRIMARY KEY,
    first_seen REAL DEFAULT 0,
    last_posted REAL DEFAULT 0,
    post_count INTEGER DEFAULT 1,
    titles TEXT DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_topic_registry_last ON topic_registry(last_posted);
"""


async def init_db() -> None:
    """Initialize database — create all tables."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()


async def get_or_create_user(user_id: int, username: str = "", first_name: str = "",
                              last_name: str = "", language_code: str = "ru") -> Dict[str, Any]:
    """Get or create a user record. Returns user dict.
    
    Handles race condition where two concurrent requests try to INSERT
    the same user_id — uses INSERT OR IGNORE + SELECT instead of
    INSERT + catch IntegrityError.
    """
    now = time.time()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()

        if row is None:
            try:
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
            except aiosqlite.IntegrityError:
                # Race condition: another request already inserted this user
                # Re-fetch the existing row
                async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cursor:
                    row = await cursor.fetchone()
                if row:
                    # Still update last_seen
                    await db.execute(
                        """UPDATE users SET username=?, first_name=?, last_name=?,
                           last_seen=?, message_count=message_count+1 WHERE user_id=?""",
                        (username, first_name, last_name, now, user_id),
                    )
                    await db.commit()
                    return dict(row)
                # Shouldn't happen, but fallback
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
                         published: float = 0, category: str = "auto", lang: str = "ru",
                         image_urls: list = None) -> bool:
    """Add a news item. Returns True if new, False if duplicate.
    
    image_urls: list of image URL strings extracted from the RSS feed.
    """
    now = time.time()
    image_urls_json = json.dumps(image_urls or [], ensure_ascii=False)
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """INSERT INTO news_items (source, title, url, summary, published, fetched_at, category, lang, image_urls)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (source, title, url, summary, published, now, category, lang, image_urls_json),
            )
            await db.commit()
            return True
    except aiosqlite.IntegrityError:
        # If already exists, update image_urls if we have new ones
        if image_urls:
            try:
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute(
                        "UPDATE news_items SET image_urls = ? WHERE url = ? AND image_urls = '[]'",
                        (image_urls_json, url),
                    )
                    await db.commit()
            except Exception:
                pass
        return False


async def get_unposted_news(limit: int = 10, category: str = "") -> List[Dict[str, Any]]:
    """Get unposted news items, ordered by publish date.
    
    Parses image_urls JSON field back into a Python list.
    """
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
        result = []
        for r in rows:
            item = dict(r)
            # Parse image_urls from JSON string to list
            if "image_urls" in item and isinstance(item["image_urls"], str):
                try:
                    item["image_urls"] = json.loads(item["image_urls"])
                except (json.JSONDecodeError, TypeError):
                    item["image_urls"] = []
            elif "image_urls" not in item:
                item["image_urls"] = []
            result.append(item)
        return result


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


async def get_hourly_post_count() -> int:
    """Get number of posts made in the last hour."""
    hour_ago = time.time() - 3600
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM channel_posts WHERE created_at >= ?", (hour_ago,),
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


async def add_user_car(user_id: int, brand: str = "", model: str = "", year: int = 0,
                       vin: str = "", engine: str = "", mileage: int = 0,
                       notes: str = "") -> int:
    """Add a car to user's profile. Returns car ID."""
    now = time.time()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO user_cars (user_id, brand, model, year, vin, engine, mileage, notes, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, brand, model, year, vin, engine, mileage, notes, now),
        )
        await db.commit()
        return cursor.lastrowid


async def get_user_cars(user_id: int) -> List[Dict[str, Any]]:
    """Get all cars for a user."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM user_cars WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def delete_user_car(car_id: int, user_id: int) -> bool:
    """Delete a car from user's profile. Returns True if deleted."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM user_cars WHERE id = ? AND user_id = ?",
            (car_id, user_id),
        )
        await db.commit()
        return cursor.rowcount > 0


async def update_car_mileage(car_id: int, user_id: int, mileage: int) -> bool:
    """Update mileage for a user's car."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE user_cars SET mileage = ? WHERE id = ? AND user_id = ?",
            (mileage, car_id, user_id),
        )
        await db.commit()
        return cursor.rowcount > 0


# ── Rate Limiting ────────────────────────────────────────────────────────────────

_user_message_times: Dict[int, List[float]] = {}
RATE_LIMIT_MESSAGES = 10  # Max messages per minute
RATE_LIMIT_WINDOW = 60    # 1 minute window


def check_rate_limit(user_id: int) -> bool:
    """Check if user is within rate limits. Returns True if allowed."""
    now = time.time()
    if user_id not in _user_message_times:
        _user_message_times[user_id] = [now]
        return True

    # Clean old timestamps
    times = _user_message_times[user_id]
    times = [t for t in times if now - t < RATE_LIMIT_WINDOW]
    times.append(now)
    _user_message_times[user_id] = times

    return len(times) <= RATE_LIMIT_MESSAGES


async def add_post_fingerprint(title: str, content: str, post_id: int = 0) -> None:
    """Add a fingerprint for a posted item to prevent duplicates.
    
    Stores:
    - title_hash: SHA256 of normalized title (lowercase, no punctuation)
    - content_hash: SHA256 of first 500 chars of normalized content
    - title_prefix: first 30 chars of title for quick prefix matching
    """
    now = time.time()
    title_hash = _make_title_hash(title)
    content_hash = _make_content_hash(content)
    title_prefix = _normalize_text(title)[:30]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO post_fingerprints (title_hash, content_hash, title_prefix, post_id, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (title_hash, content_hash, title_prefix, post_id, now),
        )
        await db.commit()


async def is_duplicate_post(title: str, content: str = "", hours: int = 48,
                            source_url: str = "") -> bool:
    """Check if a post with similar title or content was recently posted.

    Checks (in order):
    1. Source URL match (same article from different RSS feeds)
    2. Exact title hash match (same title, possibly different source)
    3. Title prefix match (first 30 chars — catches reworded titles about same topic)
    4. Keyword overlap match (extract key nouns — catches paraphrased titles about same subject)
       Also normalizes car brand names (BMW/Bayerische, LADA/ВАЗ, etc.)
    5. Content hash match (same content, possibly different title)

    Args:
        title: News item title to check
        content: Post content to check (optional)
        hours: How many hours back to check (default 48h)
        source_url: Original article URL for URL-based dedup (optional)

    Returns:
        True if a similar post was found (DUPLICATE), False if unique
    """
    cutoff = time.time() - (hours * 3600)
    title_hash = _make_title_hash(title)
    title_prefix = _normalize_text(title)[:30]

    async with aiosqlite.connect(DB_PATH) as db:
        # Check 1: Source URL match — same article from different RSS feeds
        # Compares the path component of URLs (ignores www/http/https differences)
        if source_url:
            url_fingerprint = _make_url_fingerprint(source_url)
            if url_fingerprint:
                async with db.execute(
                    "SELECT source_url FROM channel_posts WHERE created_at >= ?",
                    (cutoff,),
                ) as cursor:
                    rows = await cursor.fetchall()
                    for row in rows:
                        existing_url = row[0] if row else ""
                        if existing_url and _make_url_fingerprint(existing_url) == url_fingerprint:
                            return True

        # Check 2: Exact title hash match
        async with db.execute(
            "SELECT COUNT(*) FROM post_fingerprints WHERE title_hash = ? AND created_at >= ?",
            (title_hash, cutoff),
        ) as cursor:
            row = await cursor.fetchone()
            if row and row[0] > 0:
                return True

        # Check 3: Title prefix match (catches reworded titles about same topic)
        if len(title_prefix) >= 10:
            async with db.execute(
                "SELECT COUNT(*) FROM post_fingerprints WHERE title_prefix = ? AND created_at >= ?",
                (title_prefix, cutoff),
            ) as cursor:
                row = await cursor.fetchone()
                if row and row[0] > 0:
                    return True

        # Check 4: Keyword overlap — extract significant words from title and compare
        # Also normalizes car brand names (BMW/Bayerische, LADA/ВАЗ, etc.)
        title_keywords = _extract_title_keywords(title)
        title_keywords_normalized = _normalize_brand_keywords(title_keywords)
        if len(title_keywords_normalized) >= 2:
            async with db.execute(
                "SELECT title_prefix FROM post_fingerprints WHERE created_at >= ?",
                (cutoff,),
            ) as cursor:
                rows = await cursor.fetchall()
                for row in rows:
                    existing_prefix = row[0] if row else ""
                    if not existing_prefix:
                        continue
                    existing_keywords = _extract_title_keywords(existing_prefix)
                    existing_keywords_normalized = _normalize_brand_keywords(existing_keywords)
                    if not existing_keywords_normalized:
                        continue
                    # Calculate keyword overlap ratio (using normalized brands)
                    common = title_keywords_normalized & existing_keywords_normalized
                    if len(common) >= 2:
                        overlap_ratio = len(common) / min(len(title_keywords_normalized), len(existing_keywords_normalized))
                        if overlap_ratio >= 0.55:
                            return True
                    # Also check overlap between raw keywords for better matching
                    raw_common = title_keywords & existing_keywords
                    if len(raw_common) >= 3:
                        raw_overlap_ratio = len(raw_common) / min(len(title_keywords), len(existing_keywords)) if min(len(title_keywords), len(existing_keywords)) > 0 else 0
                        if raw_overlap_ratio >= 0.5:
                            return True

        # Check 5: Content hash match (if content provided)
        if content:
            content_hash = _make_content_hash(content)
            async with db.execute(
                "SELECT COUNT(*) FROM post_fingerprints WHERE content_hash = ? AND created_at >= ?",
                (content_hash, cutoff),
            ) as cursor:
                row = await cursor.fetchone()
                if row and row[0] > 0:
                    return True

    return False


async def get_recent_post_titles(hours: int = 48, limit: int = 50) -> List[str]:
    """Get titles of recently posted news items for similarity checking."""
    cutoff = time.time() - (hours * 3600)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT content, source_url FROM channel_posts
               WHERE created_at >= ? AND post_type = 'news'
               ORDER BY created_at DESC LIMIT ?""",
            (cutoff, limit),
        ) as cursor:
            rows = await cursor.fetchall()
            # Extract titles from post content (first line usually)
            titles = []
            for row in rows:
                content = row["content"] if "content" in row.keys() else row[0]
                if content:
                    first_line = content.split('\n')[0].strip()
                    if first_line:
                        titles.append(first_line)
            return titles


def _normalize_text(text: str) -> str:
    """Normalize text for comparison: lowercase, remove punctuation, collapse spaces."""
    import re
    text = text.lower().strip()
    # Remove punctuation
    text = re.sub(r'[^\w\sа-яё]', '', text, flags=re.IGNORECASE)
    # Collapse whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    return text


# Stopwords to exclude from keyword extraction (common words that don't carry meaning)
_TITLE_STOPWORDS = {
    # English
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "need", "must", "ought",
    "to", "of", "in", "for", "on", "with", "at", "by", "from", "as",
    "into", "through", "during", "before", "after", "above", "below",
    "between", "out", "off", "over", "under", "again", "further",
    "then", "once", "and", "but", "or", "nor", "not", "so", "yet",
    "both", "either", "neither", "each", "every", "all", "any",
    "this", "that", "these", "those", "it", "its", "he", "she",
    "they", "them", "we", "you", "i", "me", "my", "your", "his",
    "her", "our", "their", "what", "which", "who", "whom", "how",
    "when", "where", "why", "if", "than", "too", "very", "just",
    "about", "also", "more", "most", "other", "some", "such",
    "no", "only", "own", "same", "up", "new", "now", "here",
    "get", "gets", "got", "make", "makes", "take", "takes",
    # Russian
    "и", "в", "во", "не", "что", "он", "на", "я", "с", "со", "как",
    "а", "то", "все", "она", "так", "его", "но", "да", "ты", "к",
    "у", "же", "вы", "за", "бы", "по", "только", "ее", "мне", "было",
    "вот", "от", "меня", "еще", "нет", "о", "из", "ему", "теперь",
    "когда", "даже", "ну", "вдруг", "ли", "если", "уже", "или", "ни",
    "быть", "был", "него", "до", "вас", "нибудь", "опять", "уж",
    "ведь", "там", "потом", "себя", "ничего", "ей", "может", "они",
    "тут", "где", "есть", "надо", "ней", "для", "мы", "тебя", "их",
    "чем", "была", "сам", "чтоб", "без", "будто", "человек", "чего",
    "раз", "тоже", "себе", "под", "будет", "ж", "тогда", "кто",
    "этот", "того", "потому", "этого", "какой", "совсем", "ним",
    "здесь", "этом", "один", "почти", "мой", "тем", "чтобы", "нее",
    "сейчас", "были", "куда", "зачем", "всех", "никогда", "можно",
    "при", "наконец", "два", "об", "другой", "хоть", "после",
    "над", "больше", "тот", "через", "эти", "нас", "про", "всего",
    "них", "какая", "много", "разве", "три", "эту", "моя", "впрочем",
    "хорошо", "свою", "этой", "перед", "иногда", "лучше", "чуть",
    "том", "нельзя", "такой", "им", "более", "всегда", "конечно",
    "всю", "между",
}


def _extract_title_keywords(title: str) -> set:
    """Extract significant keywords from a title for overlap comparison.

    Returns a set of normalized significant words (stopwords removed, short words filtered).
    """
    import re
    normalized = _normalize_text(title)
    words = set(normalized.split())
    # Remove stopwords and very short words (less than 3 chars)
    significant = set()
    for w in words:
        if len(w) >= 3 and w not in _TITLE_STOPWORDS:
            significant.add(w)
    return significant


# Car brand name normalization map for dedup
_BRAND_NORMALIZE_MAP = {
    # Russian → English
    "бмв": "bmw", "бавария": "bmw", "байерише": "bmw",
    "мерседес": "mercedes", "мерс": "mercedes",
    "фольксваген": "volkswagen", "ваг": "vw", "фв": "vw",
    "ауди": "audi",
    "тойота": "toyota",
    "ниссан": "nissan",
    "хонда": "honda",
    "мазда": "mazda",
    "киа": "kia",
    "хендай": "hyundai", "хёндэ": "hyundai", "хундай": "hyundai",
    "форд": "ford",
    "рено": "renault",
    "пежо": "peugeot",
    "шевроле": "chevrolet", "шеви": "chevy",
    "кадиллак": "cadillac",
    "лексус": "lexus",
    "инфинити": "infiniti",
    "порше": "porsche",
    "вольво": "volvo",
    "субару": "subaru",
    "сузуки": "suzuki",
    "митсубиси": "mitsubishi", "мицубиси": "mitsubishi",
    "шкода": "skoda",
    "сеат": "seat",
    "фиат": "fiat",
    "альфа": "alfa",
    "ягуар": "jaguar",
    "лендровер": "landrover", "лэндровер": "landrover",
    "миникар": "mini",
    "смарт": "smart",
    "опель": "opel",
    "ваз": "lada", "лава": "lada",
    "уаз": "uaz",
    "газ": "gaz",
    "черри": "chery",
    "хавал": "haval",
    "джили": "geely",
    "чанган": "changan",
    "эксид": "exeed",
    "танк": "tank",
    "тесла": "tesla",
    "байд": "byd",
    "зикр": "zeekr",
    "лисян": "lixiang", "ли": "li",
    # English alternate spellings
    "vw": "volkswagen", "chevy": "chevrolet",
    "benz": "mercedes", "daimler": "mercedes",
    "beemer": "bmw", "bimmer": "bmw",
}


def _normalize_brand_keywords(keywords: set) -> set:
    """Normalize car brand names in keyword set for better dedup matching.
    
    Maps alternate brand spellings to a canonical form so that
    'BMW X5 новый двигатель' and 'БМВ Х5 получила мотор' are detected as duplicates.
    """
    normalized = set()
    for kw in keywords:
        kw_lower = kw.lower()
        if kw_lower in _BRAND_NORMALIZE_MAP:
            normalized.add(_BRAND_NORMALIZE_MAP[kw_lower])
        else:
            normalized.add(kw)
    return normalized


def _make_url_fingerprint(url: str) -> str:
    """Create a fingerprint of a URL for deduplication.
    
    Strips protocol, www prefix, trailing slashes, and query parameters
    so that https://www.example.com/article/123 and http://example.com/article/123/
    are treated as the same URL.
    """
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url.strip())
        # Use path only (ignore protocol, www, query, fragment)
        path = parsed.path.rstrip('/')
        domain = parsed.netloc.lower()
        # Remove www prefix
        if domain.startswith('www.'):
            domain = domain[4:]
        fingerprint_str = f"{domain}{path}"
        if not fingerprint_str or len(fingerprint_str) < 5:
            return ""
        return hashlib.sha256(fingerprint_str.encode('utf-8')).hexdigest()
    except Exception:
        return ""


def _make_title_hash(title: str) -> str:
    """Create a hash of normalized title for exact duplicate detection."""
    normalized = _normalize_text(title)
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()


def _make_content_hash(content: str) -> str:
    """Create a hash of normalized content (first 500 chars) for duplicate detection."""
    normalized = _normalize_text(content[:500])
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()


async def cleanup_old_fingerprints(max_age_days: int = 7) -> int:
    """Remove fingerprints older than max_age_days. Returns count of removed rows."""
    cutoff = time.time() - (max_age_days * 86400)
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM post_fingerprints WHERE created_at < ?", (cutoff,)
        )
        await db.commit()
        return cursor.rowcount


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


# ── Topic Registry (persistent) ──────────────────────────────────────────────

async def load_topic_registry() -> Dict:
    """Load topic registry from DB into memory. Returns dict of entity_key -> {first_seen, last_posted, post_count, titles}.

    Only loads topics that are NOT expired (within 72h of last_posted).
    This is called at startup to restore the registry after restart.
    """
    max_age = 48 * 3600  # 48 hours — faster topic cycling
    cutoff = time.time() - max_age
    registry = {}
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT entity_key, first_seen, last_posted, post_count, titles FROM topic_registry WHERE last_posted >= ?",
                (cutoff,),
            ) as cursor:
                rows = await cursor.fetchall()
                for row in rows:
                    entity_key, first_seen, last_posted, post_count, titles_json = row
                    try:
                        titles = json.loads(titles_json) if titles_json else []
                    except (json.JSONDecodeError, TypeError):
                        titles = []
                    registry[entity_key] = {
                        "first_seen": first_seen,
                        "last_posted": last_posted,
                        "post_count": post_count,
                        "titles": titles,
                    }
    except Exception as e:
        # Table might not exist yet on first run — that's OK
        import logging
        logging.getLogger("asya.database").debug(f"Could not load topic registry: {e}")
    return registry


async def save_topic_to_registry(entity_key: str, first_seen: float, last_posted: float,
                                  post_count: int, titles: list) -> None:
    """Save or update a single topic in the DB registry."""
    if not entity_key:
        return
    titles_json = json.dumps(titles[-20:], ensure_ascii=False)  # Keep last 20 titles
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """INSERT INTO topic_registry (entity_key, first_seen, last_posted, post_count, titles)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(entity_key) DO UPDATE SET
                       last_posted = excluded.last_posted,
                       post_count = excluded.post_count,
                       titles = excluded.titles""",
                (entity_key, first_seen, last_posted, post_count, titles_json),
            )
            await db.commit()
    except Exception as e:
        import logging
        logging.getLogger("asya.database").debug(f"Could not save topic to registry: {e}")


async def cleanup_topic_registry(max_age_hours: int = 48) -> int:
    """Remove expired topics from the DB registry. Returns count of removed rows."""
    cutoff = time.time() - (max_age_hours * 3600)
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "DELETE FROM topic_registry WHERE last_posted < ?", (cutoff,)
            )
            await db.commit()
            return cursor.rowcount
    except Exception:
        return 0
