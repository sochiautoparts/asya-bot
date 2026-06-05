"""Channel Scanner v1.0 - Scan public Telegram channel for dedup (Asya bot).

Scrapes t.me/s/sochiautoparts (public web version) to get
the last 30-50 post texts. Used before posting to @sochiautoparts
to prevent duplicate news.

HOW IT WORKS:
  - HTTP GET to https://t.me/s/sochiautoparts
  - Parse HTML for div.tgme_widget_message_text elements
  - Cache results for 10 minutes to avoid excessive requests
"""

import asyncio
import logging
import re
import time
from typing import Dict, List, Optional, Set

import httpx

logger = logging.getLogger("asya.channel_scanner")

# Cache settings
_CACHE_TTL = 600  # 10 minutes
_cached_posts: List[str] = []
_cached_fingerprints: Set[str] = set()
_cache_time: float = 0

# Target channel
CHANNEL_WEB_URL = "https://t.me/s/sochiautoparts"


def _compute_fingerprint(text: str) -> str:
    """Compute a fingerprint for dedup: lowercase, strip punctuation, first 8 words."""
    text_lower = text.lower().strip()
    text_lower = re.sub(r'\b(в|на|с|о|у|по|из|за|от|до|к|не|и|но|а|что|как|это|тот|этот|для|при|же|ли|бы|уже|ещё|еще|также|тоже|или)\b', '', text_lower)
    text_lower = re.sub(r'[^a-zа-яё0-9\s]', '', text_lower)
    words = text_lower.split()[:8]
    return ' '.join(words)


async def fetch_channel_posts(max_posts: int = 50) -> List[str]:
    """Fetch recent post texts from the public web version of the channel.

    Returns list of post text strings (newest first).
    Caches results for 10 minutes.
    """
    global _cached_posts, _cached_fingerprints, _cache_time

    # Return cache if fresh
    if _cached_posts and (time.time() - _cache_time) < _CACHE_TTL:
        logger.info(f"Channel scanner: using cached posts ({len(_cached_posts)} posts)")
        return _cached_posts[:max_posts]

    posts: List[str] = []
    last_msg_id: Optional[int] = None

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=5.0),
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; AsyaBot/6.0; Channel Scanner)",
                "Accept": "text/html",
            },
        ) as client:
            for page in range(3):
                url = CHANNEL_WEB_URL
                if last_msg_id:
                    url += f"?before={last_msg_id}"

                response = await client.get(url)
                if response.status_code != 200:
                    logger.warning(f"Channel scanner: HTTP {response.status_code} for {url}")
                    break

                html = response.text

                text_pattern = re.compile(
                    r'<div\s+class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
                    re.DOTALL | re.IGNORECASE
                )

                page_posts = []
                for match in text_pattern.finditer(html):
                    raw_text = match.group(1)
                    clean = re.sub(r'<[^>]+>', '', raw_text).strip()
                    clean = clean.replace('&amp;', '&').replace('&lt;', '<')
                    clean = clean.replace('&gt;', '>').replace('&quot;', '"')
                    clean = clean.replace('&#39;', "'").replace('&nbsp;', ' ')
                    clean = re.sub(r'\s+', ' ', clean).strip()
                    if clean and len(clean) > 10:
                        page_posts.append(clean)

                posts.extend(page_posts)

                msg_id_pattern = re.compile(r'data-post="[^/]+/(\d+)"')
                msg_ids = [int(m) for m in msg_id_pattern.findall(html)]
                if msg_ids:
                    last_msg_id = min(msg_ids)
                else:
                    break

                if len(posts) >= max_posts:
                    break

                await asyncio.sleep(0.5)

    except Exception as e:
        logger.warning(f"Channel scanner error: {e}")

    if posts:
        _cached_posts = posts
        _cached_fingerprints = {_compute_fingerprint(p) for p in posts}
        _cache_time = time.time()

    logger.info(f"Channel scanner: fetched {len(posts)} posts from {CHANNEL_WEB_URL}")
    return posts[:max_posts]


async def is_duplicate_in_channel(text: str, threshold: float = 0.55) -> bool:
    """Check if a text is a duplicate of recent channel posts.

    Args:
        text: The text to check
        threshold: Overlap threshold (0-1). 0.55 = 55% word overlap = duplicate.

    Returns:
        True if the text appears to be a duplicate
    """
    global _cached_fingerprints

    if not _cached_posts or (time.time() - _cache_time) > _CACHE_TTL:
        await fetch_channel_posts()

    fp = _compute_fingerprint(text)
    if fp in _cached_fingerprints:
        logger.info(f"Channel dedup: fingerprint match for '{text[:60]}...'")
        return True

    text_words = set(text.lower().split())
    if not text_words:
        return False

    for cached_post in _cached_posts:
        cached_words = set(cached_post.lower().split())
        if not cached_words:
            continue

        intersection = text_words & cached_words
        union = text_words | cached_words
        if not union:
            continue

        overlap = len(intersection) / len(union)
        if overlap >= threshold:
            logger.info(f"Channel dedup: word overlap {overlap:.0%} with '{cached_post[:60]}...'")
            return True

    return False


async def get_channel_context_for_prompt(max_items: int = 10) -> str:
    """Get recent channel posts as context for AI prompt.

    Returns:
        String with recent post summaries for AI context.
    """
    posts = await fetch_channel_posts(max_posts=max_items)
    if not posts:
        return ""

    summaries = []
    for i, post in enumerate(posts[:max_items], 1):
        short = post[:80] + ("..." if len(post) > 80 else "")
        summaries.append(f"{i}. {short}")

    return (
        "ПОСЛЕДНИЕ ПОСТЫ В КАНАЛЕ @sochiautoparts (НЕ ПОВТОРЯЙ!):\n"
        + "\n".join(summaries)
    )
