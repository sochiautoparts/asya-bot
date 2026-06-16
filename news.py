"""
News Engine v3.0 — Single-Source JSON Fetcher
Fetches pre-parsed automotive news from sochiautoparts/nws repository.
No RSS parsing, no image extraction — all done by the external parser.

ARCHITECTURE:
  External parser (sochiautoparts/nws) → data/auto-news.json (GitHub Raw)
  → This module fetches JSON → loads into DB → ready for posting

The external parser runs hourly via GitHub Actions and produces:
  - Title, summary, source URL, single image URL
  - Deduplication already done
  - Language detection done by this module from title
  - Wrapped in metadata object with {kind, generated_at, total_items, items: [...]}

This module just fetches and stores — fast, reliable, no heavy lifting.

v3.0 CHANGES:
  - Changed news source to sochiautoparts/nws/data/auto-news.json
  - New JSON format: items under "items" key in wrapper object with metadata
  - Single "image" field instead of "images" array (already supported by normalizer)
  - No "lang" field — language detected from title by _detect_language()
  - Added "id" and "source_url" field support from new format
  - Removed separate ru-news.json source (auto-news.json contains all auto news)
"""
import httpx
import json
import time
import logging
import re
from html import unescape as html_unescape
from typing import List, Dict, Optional
from datetime import datetime
from collections import OrderedDict

from bot.config import config
from bot.database import add_news_item, get_unposted_news, mark_news_posted, is_duplicate_post

logger = logging.getLogger("asya.news")

# ── Source JSON URLs ────────────────────────────────────────────────────────────
# sochiautoparts/nws repository — auto news from 14+ curated RSS sources
NEWS_JSON_URL = "https://raw.githubusercontent.com/sochiautoparts/nws/main/data/auto-news.json"

NEWS_JSON_FALLBACK_URLS = [
    NEWS_JSON_URL,
    # Legacy fallback — creastudioai-beep/news (deprecated, may be unavailable)
    "https://raw.githubusercontent.com/creastudioai-beep/news/refs/heads/main/data/news.json",
]
FETCH_TIMEOUT = 30.0
MAX_NEWS_PER_CYCLE = 2000  # Process ALL items — user wants selection from FULL array

# ── Fingerprint-based deduplication ────────────────────────────────────────────
# Using OrderedDict to maintain insertion order — oldest entries removed first
_recent_fingerprints: OrderedDict = OrderedDict()


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
    """Check if a fingerprint matches any recently used fingerprint.
    
    v2.1: Raised match threshold from 3 to 4 words to reduce false positives.
    With 3 words, common automotive titles like "BMW X5 new engine" and 
    "BMW X5 recalled engine" were incorrectly matching as duplicates.
    """
    fp_words = fingerprint.split()[:5]
    if len(fp_words) < 2:
        return False
    for existing in _recent_fingerprints:
        ex_words = existing.split()[:5]
        matches = sum(1 for w in fp_words if w in ex_words)
        # v2.2: Require EXACT 5/5 word match for dedup — 4/5 was still too aggressive
        # causing different news about same car model to be blocked
        if matches >= 5 and len(fp_words) >= 5:
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
    """Fetch news JSON from the sochiautoparts/nws repository.
    
    Returns a list of news items from all sources, deduplicated by URL.
    Each item has: title, summary, url, source, image, published
    
    Supported JSON formats:
    - New format: {"kind": "auto", "items": [...], "total_items": N, ...}
    - Old format: [{...}, {...}] (flat list)
    - Legacy format: {"news": [...]} (dict with news key)
    """
    all_items = []
    seen_urls = set()
    
    for url in NEWS_JSON_FALLBACK_URLS:
        try:
            async with httpx.AsyncClient(
                timeout=FETCH_TIMEOUT,
                follow_redirects=True,
                headers={
                    "User-Agent": "AsyaBot/2.1 NewsFetcher",
                    "Accept": "application/json",
                },
            ) as client:
                response = await client.get(url)
                if response.status_code == 200:
                    data = response.json()
                    items = []
                    if isinstance(data, list):
                        items = data
                    elif isinstance(data, dict):
                        # New sochiautoparts/nws format: items under "items" key
                        if "items" in data:
                            items = data["items"]
                            meta_total = data.get("total_items", len(items))
                            generated_at = data.get("generated_at", "")
                            sources_count = data.get("sources_count", 0)
                            logger.info(
                                f"Metadata: {meta_total} items, generated at {generated_at}, "
                                f"{sources_count} sources"
                            )
                        # Legacy format: items under "news" key
                        elif "news" in data:
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
    
    Sochiautoparts/nws format (current):
    {
        "id": "bb7defca8f5cbcbe",
        "title": "...",
        "summary": "...",
        "url": "https://...",
        "image": "https://...",     # Single image (string)
        "source": "Car and Driver",
        "source_url": "https://...",
        "published": "2026-06-16T19:44:00+00:00"
    }
    
    Legacy creastudioai-beep/news format (fallback):
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
                    # CRITICAL FIX: Decode HTML entities in image URLs
                    # Some sources have &amp; instead of & which breaks the URL
                    decoded_img = html_unescape(html_unescape(img))
                    image_urls.append(decoded_img)
                elif isinstance(img, dict):
                    # Some formats use {"url": "..."} for images
                    img_url = img.get("url", "")
                    if img_url and img_url.startswith("http"):
                        decoded_url = html_unescape(html_unescape(img_url))
                        image_urls.append(decoded_url)

    # Single image field — handle both string and list formats
    single_image = item.get("image", "") or item.get("thumbnail", "") or item.get("featured_image", "")
    if single_image:
        if isinstance(single_image, str) and single_image.startswith("http"):
            decoded_single = html_unescape(html_unescape(single_image))
            image_urls.insert(0, decoded_single)
        elif isinstance(single_image, list):
            # Handle "image": ["url1", "url2"] format
            for img in single_image:
                if isinstance(img, str) and img.startswith("http"):
                    decoded_img = html_unescape(html_unescape(img))
                    image_urls.append(decoded_img)
                elif isinstance(img, dict):
                    img_url = img.get("url", "")
                    if img_url and img_url.startswith("http"):
                        decoded_url = html_unescape(html_unescape(img_url))
                        image_urls.append(decoded_url)

    # Deduplicate image URLs
    # v2.2: Enhanced dedup — also dedup by base URL (without query params)
    # because ?resize=640:* and ?resize=100:* and ?crop=0.5xw are the SAME image
    seen = set()
    seen_base = set()  # Base URLs without query params for dedup
    unique_images = []
    for img_url in image_urls:
        if img_url not in seen:
            # Get base URL without query params for dedup
            base_url = img_url.split('?')[0]
            if base_url not in seen_base:
                seen.add(img_url)
                seen_base.add(base_url)
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
    
    v2.1 CHANGES:
    - Reduced is_duplicate_post window from 168h (7d) to 72h (3d)
    - Removed redundant semantic dedup check (already done in channel.py)
    - Increased MAX_NEWS_PER_CYCLE to 50
    """
    logger.info("Starting news cycle — fetching from external JSON sources")

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

    # Sort by published date (newest first) if available — ISO format sorts lexicographically
    items.sort(key=lambda item: item.get("published", ""), reverse=True)

    # Limit items per cycle
    items = items[:MAX_NEWS_PER_CYCLE]

    for item in items:
        title = item["title"]
        url = item["url"]

        # Skip if URL already in DB — reduced window from 7d to 3d
        # (7 days was too aggressive, blocking valid news that refreshed)
        try:
            if await is_duplicate_post(title, hours=48):  # 2 days dedup window (was 72h/3d — reduced for higher throughput)
                duplicates += 1
                continue
        except Exception:
            pass

        # Fingerprint dedup (v2.1: less aggressive matching)
        fingerprint = _compute_fingerprint(title)
        if _fingerprint_matches_existing(fingerprint):
            duplicates += 1
            continue

        # NOTE: Removed semantic dedup check here — it's already done in channel.py
        # at posting time (Layer 2). Running it here caused false blocks because
        # news titles often share keywords but are about different events.
        # The channel.py dedup is sufficient and more accurate at post time.

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
            _recent_fingerprints[fingerprint] = True
            # Keep fingerprint OrderedDict bounded — remove oldest first
            # v2.2: Increased from 500→1000 to handle full news volume
            if len(_recent_fingerprints) > 1000:
                # Remove oldest entries (first inserted in OrderedDict)
                excess = len(_recent_fingerprints) - 800
                for _ in range(excess):
                    _recent_fingerprints.popitem(last=False)  # Remove oldest
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
