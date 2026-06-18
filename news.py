"""
News Engine v3.1 — Single-Source JSON Fetcher
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

v3.1 CHANGES (audit pass):
  - SINGLE SOURCE: removed legacy fallback URL list.
    The only news source is now sochiautoparts/nws/data/auto-news.json
    as the user requires.
  - Increased FETCH_TIMEOUT from 30s to 60s — the auto-news.json file
    is ~450 KB (500 items × 61 sources) and sometimes takes 30-50s to
    download from raw.githubusercontent.com.
  - Added a single retry on timeout (1 attempt after 5s wait).
  - Robust handling of "image" (string) AND "images" (array) fields —
    the source provides both. _normalize_news_item merges them and
    deduplicates by base URL (without query params).
  - Better logging: per-source attempt, size, item count.
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

# ── Source JSON URL (SINGLE SOURCE — sochiautoparts/nws) ──────────────────────
# v3.1: The ONLY news source. The legacy fallback URL list was removed.
# If the primary source fails, we log and retry next cycle (every 15 min).
NEWS_JSON_URL = "https://raw.githubusercontent.com/sochiautoparts/nws/main/data/auto-news.json"

# v3.1: No fallback URLs — single source as required.
# If the fetch fails, returns None and the caller logs + retries next cycle.
FETCH_TIMEOUT = 60.0  # v3.1: was 30s — file is ~450KB, sometimes slow from GitHub
FETCH_RETRY_DELAY = 5.0  # seconds to wait before retry on timeout
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
    """Fetch news JSON from the sochiautoparts/nws repository (SINGLE SOURCE).
    
    v3.1: Single source — no fallbacks. If the fetch fails, returns None
    and the caller logs + retries next cycle.
    
    Returns a list of news items, deduplicated by URL.
    Each item has: id, title, summary, url, image, images, source, source_url, published
    
    Source JSON format:
        {
          "kind": "auto",
          "generated_at": "2026-...",
          "total_items": 500,
          "sources_count": 61,
          "items": [
            {
              "id": "...",
              "title": "...",
              "summary": "...",
              "url": "https://...",
              "image": "https://...",   # single image (string)
              "images": ["https://..."],  # array of images
              "source": "Car and Driver",
              "source_url": "https://...",
              "published": "2026-06-18T09:06:35+00:00"
            }
          ]
        }
    """
    all_items = []
    seen_urls = set()
    
    # v3.1: Single source with one retry on timeout
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(
                timeout=FETCH_TIMEOUT,
                follow_redirects=True,
                headers={
                    "User-Agent": "AsyaBot/3.1 NewsFetcher",
                    "Accept": "application/json",
                    "Cache-Control": "no-cache",
                },
            ) as client:
                logger.info(f"Fetching news JSON from {NEWS_JSON_URL} (attempt {attempt+1}/2)")
                response = await client.get(NEWS_JSON_URL)
                if response.status_code == 200:
                    data = response.json()
                    items = []
                    if isinstance(data, list):
                        # Flat list format (older format)
                        items = data
                    elif isinstance(data, dict):
                        # New sochiautoparts/nws format: items under "items" key
                        if "items" in data:
                            items = data["items"]
                            meta_total = data.get("total_items", len(items))
                            generated_at = data.get("generated_at", "")
                            sources_count = data.get("sources_count", 0)
                            multi_photo = data.get("multi_photo_items", 0)
                            logger.info(
                                f"Metadata: {meta_total} items, generated at {generated_at}, "
                                f"{sources_count} sources, {multi_photo} multi-photo items"
                            )
                        # Legacy dict format: items under "news" key
                        elif "news" in data:
                            items = data["news"]
                            logger.info(f"Legacy dict format: {len(items)} items")
                    
                    # Deduplicate by URL within this batch
                    for item in items:
                        item_url = item.get("url", "")
                        if item_url and item_url not in seen_urls:
                            seen_urls.add(item_url)
                            all_items.append(item)
                    
                    logger.info(
                        f"Fetched {len(items)} items from sochiautoparts/nws "
                        f"({len(all_items)} unique after URL dedup)"
                    )
                    # Success — break out of retry loop
                    break
                else:
                    logger.warning(
                        f"HTTP {response.status_code} from {NEWS_JSON_URL} "
                        f"(attempt {attempt+1}/2)"
                    )
                    if attempt == 0:
                        await asyncio_sleep(FETCH_RETRY_DELAY)
                        continue
        except httpx.TimeoutException:
            logger.warning(
                f"Timeout fetching from {NEWS_JSON_URL} (attempt {attempt+1}/2, "
                f"timeout={FETCH_TIMEOUT}s)"
            )
            if attempt == 0:
                await asyncio_sleep(FETCH_RETRY_DELAY)
                continue
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON from {NEWS_JSON_URL}: {e}")
            # Don't retry — bad JSON won't fix itself
            return None
        except Exception as e:
            logger.error(f"Error fetching from {NEWS_JSON_URL}: {e}")
            if attempt == 0:
                await asyncio_sleep(FETCH_RETRY_DELAY)
                continue
    
    if not all_items:
        logger.error(
            "News fetch failed (single source sochiautoparts/nws unavailable). "
            "Will retry next cycle (every 15 min)."
        )
        return None
    
    logger.info(f"Returning {len(all_items)} unique news items")
    return all_items


async def asyncio_sleep(seconds: float) -> None:
    """Helper to avoid importing asyncio at module top."""
    import asyncio
    await asyncio.sleep(seconds)


def _normalize_news_item(item: Dict) -> Optional[Dict]:
    """Normalize a news item from the external JSON format.
    
    Sochiautoparts/nws format (v3.1 — current source):
    {
        "id": "bb7defca8f5cbcbe",
        "title": "...",
        "summary": "...",
        "url": "https://...",
        "image": "https://...",     # Single image (string)
        "images": ["https://..."],  # Array of images (may have multiple)
        "source": "Car and Driver",
        "source_url": "https://...",
        "published": "2026-06-16T19:44:00+00:00"
    }
    
    We normalize to the internal format expected by the bot.
    Both "image" and "images" fields are merged and deduplicated.
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

    # v3.1: Preserve id from source for better tracking (optional)
    item_id = item.get("id", "") or item.get("source_url", "")

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
        "id": item_id,
    }


async def run_news_cycle() -> int:
    """Fetch news from the external JSON source and store in DB.
    
    Returns the number of NEW items added.
    
    v3.1: Single source (sochiautoparts/nws). On fetch failure, returns 0
    and logs an error — next cycle (15 min) will retry.
    """
    logger.info("Starting news cycle — fetching from sochiautoparts/nws (single source)")

    # Fetch JSON
    raw_items = await fetch_news_json()
    if not raw_items:
        logger.warning("No news items fetched — will retry next cycle (15 min)")
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
