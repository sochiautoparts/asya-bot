"""
News Engine v2.0 — Single-Source JSON Fetcher
Fetches pre-parsed automotive news from creastudioai-beep/news repository.
No RSS parsing, no image extraction — all done by the external parser.

ARCHITECTURE:
  External parser (creastudioai-beep/news) → data/news.json (GitHub Raw)
  → This module fetches JSON → loads into DB → ready for posting

The external parser runs every hour via GitHub Actions and produces:
  - Title, summary, source URL
  - Multiple photo URLs per news item
  - Deduplication already done
  - Language detection already done

This module just fetches and stores — fast, reliable, no heavy lifting.
"""

import httpx
import json
import time
import logging
import re
from typing import List, Dict, Optional
from datetime import datetime

from bot.config import config
from bot.database import add_news_item, get_unposted_news, mark_news_posted, is_duplicate_post

logger = logging.getLogger("asya.news")

# ── Source JSON URLs ────────────────────────────────────────────────────────────
NEWS_JSON_URL = "https://raw.githubusercontent.com/creastudioai-beep/news/refs/heads/main/data/news.json"
BMW_NEWS_JSON_URL = "https://raw.githubusercontent.com/creastudioai-beep/nebm/refs/heads/main/data/news.json"
NEWS_JSON_FALLBACK_URLS = [
    NEWS_JSON_URL,
    BMW_NEWS_JSON_URL,
]
FETCH_TIMEOUT = 30.0
MAX_NEWS_PER_CYCLE = 30  # Max items to process per cycle (2 sources, fetch often, post 6/hour)

# ── Fingerprint-based deduplication ────────────────────────────────────────────
_recent_fingerprints: set = set()


def _compute_fingerprint(title: str) -> str:
    """Compute a simplified fingerprint for dedup.
    Remove articles, lowercase, take first 5 words.
    """
    cleaned = re.sub(
        r'\b(в|на|с|о|у|по|из|за|от|до|к|не|и|но|а|что|как|это|тот|этот|для|при|через|между|после|перед|без|под|над|об|со|из-за|то|же|ли|бы|уже|ещё|еще|также|тоже|или|либо|the|a|an|is|are|was|were|in|on|at|to|for|of|with|by|from|and|or|but|not|no)\b',
        '', title.lower()
    )
    cleaned = re.sub(r'[^a-zа-яё0-9]', ' ', cleaned)
    words = cleaned.split()
    return ' '.join(words[:5])


def _fingerprint_matches_existing(fingerprint: str) -> bool:
    """Check if a fingerprint matches any recently used fingerprint."""
    fp_words = fingerprint.split()[:4]
    for existing in _recent_fingerprints:
        ex_words = existing.split()[:4]
        matches = sum(1 for w in fp_words if w in ex_words)
        if matches >= 3:
            return True
    return False


def _detect_language(title: str) -> str:
    """Detect if text is primarily Russian or English."""
    russian_chars = len(re.findall(r'[а-яёА-ЯЁ]', title))
    total_chars = len(re.findall(r'[a-zA-Zа-яёА-ЯЁ]', title))
    if total_chars == 0:
        return "en"
    return "ru" if russian_chars / total_chars > 0.3 else "en"


async def fetch_news_json() -> Optional[List[Dict]]:
    """Fetch news JSON from ALL external parser repositories and merge them.
    
    Returns a MERGED list from all sources (news + nebm), deduplicated by URL.
    Each item has: title, summary, url, source, images[], published, lang
    """
    all_items = []
    seen_urls = set()
    
    for url in NEWS_JSON_FALLBACK_URLS:
        try:
            async with httpx.AsyncClient(
                timeout=FETCH_TIMEOUT,
                follow_redirects=True,
                headers={
                    "User-Agent": "AsyaBot/2.0 NewsFetcher",
                    "Accept": "application/json",
                },
            ) as client:
                response = await client.get(url)
                if response.status_code == 200:
                    data = response.json()
                    items = []
                    if isinstance(data, list):
                        items = data
                    elif isinstance(data, dict) and "news" in data:
                        items = data["news"]
                    
                    # Deduplicate by URL across sources
                    for item in items:
                        item_url = item.get("url", "")
                        if item_url and item_url not in seen_urls:
                            seen_urls.add(item_url)
                            all_items.append(item)
                    
                    logger.info(f"Fetched {len(items)} items from {url[:60]}... ({len(all_items)} total merged)")
                else:
                    logger.warning(f"HTTP {response.status_code} from {url[:60]}")
        except httpx.TimeoutException:
            logger.warning(f"Timeout fetching from {url[:60]}")
        except Exception as e:
            logger.error(f"Error fetching from {url[:60]}: {e}")

    if not all_items:
        logger.error("All news JSON sources failed")
        return None
    
    logger.info(f"Merged {len(all_items)} unique news items from all sources")
    return all_items


def _normalize_news_item(item: Dict) -> Optional[Dict]:
    """Normalize a news item from the external JSON format.
    
    The external parser produces items like:
    {
        "title": "...",
        "summary": "...",
        "url": "https://...",
        "source": "Motor1",
        "images": ["https://...", "https://..."],
        "published": "2026-06-14T10:00:00Z",
        "lang": "en"
    }
    
    We normalize to the internal format expected by the bot.
    """
    title = item.get("title", "").strip()
    if not title or len(title) < 10:
        return None

    url = item.get("url", "").strip()
    if not url:
        return None

    summary = item.get("summary", "").strip()
    if not summary:
        summary = item.get("description", "").strip()

    # Extract images — support multiple field names
    image_urls = []
    for field in ["images", "image_urls", "photos", "photos_urls"]:
        val = item.get(field, [])
        if isinstance(val, list):
            for img in val:
                if isinstance(img, str) and img.startswith("http"):
                    image_urls.append(img)
                elif isinstance(img, dict):
                    # Some formats use {"url": "..."} for images
                    img_url = img.get("url", "")
                    if img_url and img_url.startswith("http"):
                        image_urls.append(img_url)

    # Single image field
    single_image = item.get("image", "") or item.get("thumbnail", "") or item.get("featured_image", "")
    if single_image and isinstance(single_image, str) and single_image.startswith("http"):
        image_urls.insert(0, single_image)

    # Deduplicate image URLs
    seen = set()
    unique_images = []
    for img_url in image_urls:
        if img_url not in seen:
            seen.add(img_url)
            unique_images.append(img_url)

    # Detect language
    lang = item.get("lang", "") or _detect_language(title)

    # Source name
    source = item.get("source", "") or item.get("feed", "") or "unknown"

    # Category
    category = item.get("category", "auto")
    if not category:
        category = "auto"

    # Published timestamp
    published = item.get("published", "") or item.get("date", "") or item.get("pub_date", "")

    return {
        "title": title,
        "url": url,
        "summary": summary,
        "source": source,
        "category": category,
        "lang": lang,
        "image_urls": unique_images[:10],  # Max 10 images per item
        "published": published,
        "full_text": item.get("full_text", "") or item.get("content", ""),
    }


async def run_news_cycle() -> int:
    """Fetch news from the external JSON source and store in DB.
    
    Returns the number of NEW items added.
    """
    logger.info("Starting news cycle — fetching from external JSON source")

    # Fetch JSON
    raw_items = await fetch_news_json()
    if not raw_items:
        logger.warning("No news items fetched — will retry next cycle")
        return 0

    new_count = 0
    skipped = 0
    duplicates = 0

    # Process items — newest first if there's a date field
    items = []
    for raw in raw_items:
        normalized = _normalize_news_item(raw)
        if normalized:
            items.append(normalized)

    # Sort by published date (newest first) if available
    def _sort_key(item):
        pub = item.get("published", "")
        if pub:
            try:
                return pub  # ISO format sorts lexicographically
            except Exception:
                pass
        return ""

    items.sort(key=_sort_key, reverse=True)

    # Limit items per cycle
    items = items[:MAX_NEWS_PER_CYCLE]

    for item in items:
        title = item["title"]
        url = item["url"]

        # Skip if URL already in DB
        try:
            if await is_duplicate_post(title, hours=168):  # 7 days dedup window
                duplicates += 1
                continue
        except Exception:
            pass

        # Fingerprint dedup
        fingerprint = _compute_fingerprint(title)
        if _fingerprint_matches_existing(fingerprint):
            duplicates += 1
            continue

        # Also check semantic dedup from channel module
        try:
            from channel import _is_semantically_duplicate
            if _is_semantically_duplicate(title):
                duplicates += 1
                continue
        except (ImportError, AttributeError):
            pass  # Function may not exist in all versions
        except Exception:
            pass

        # Add to DB
        try:
            await add_news_item(
                title=title,
                url=url,
                summary=item.get("summary", ""),
                source=item.get("source", "unknown"),
                category=item.get("category", "auto"),
                lang=item.get("lang", "en"),
                image_urls=item.get("image_urls", []),
                published=item.get("published", ""),
            )
            _recent_fingerprints.add(fingerprint)
            # Keep fingerprint set bounded
            if len(_recent_fingerprints) > 500:
                # Remove oldest entries (set doesn't guarantee order, but that's fine)
                excess = len(_recent_fingerprints) - 300
                for _ in range(excess):
                    _recent_fingerprints.pop()
            new_count += 1
        except Exception as e:
            # May be a duplicate in DB (unique constraint on URL)
            if "UNIQUE constraint" in str(e) or "duplicate" in str(e).lower():
                skipped += 1
            else:
                logger.error(f"Error adding news item: {e}")
                skipped += 1

    logger.info(
        f"News cycle complete: {new_count} new, {duplicates} duplicates, "
        f"{skipped} skipped, {len(items)} total processed"
    )
    return new_count
