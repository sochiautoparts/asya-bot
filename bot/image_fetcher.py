"""Smart Image Fetcher v5.0 — Simple article-first image sourcing for asya-bot.

PHILOSOPHY: Automotive news articles ALREADY have photos attached.
No need for image search engines, stock photos, or AI generation.
Just take the photos from the article and attach them to the post.

PRIORITY PIPELINE:
  1. RSS images — media:content, enclosures, media:thumbnail, <img> in content
  2. Article page images — og:image, twitter:image, JSON-LD, <img> tags
  3. DONE — that's it. No search, no AI, no bullshit.

CHANGES FROM v4.0:
  - REMOVED: All image search providers (Unsplash, Pexels, Bing, Google, SearXNG)
  - REMOVED: NSFW keyword/domain filtering (unnecessary for automotive news)
  - REMOVED: AI Vision content moderation (unnecessary overhead)
  - REMOVED: AI image generation fallback (fake images worse than no image)
  - ADDED: Up to 10 images per post (Telegram mediagroup limit)
  - SIMPLIFIED: Minimal validation — just check it's a real image file
  - KEPT: SHA256 deduplication (still useful to avoid duplicate photos)
  - KEPT: Basic junk URL filtering (trackers, pixels, icons)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx

logger = logging.getLogger("asya.image_fetcher")

# ── Configuration ─────────────────────────────────────────────────────────────

IMAGE_CACHE_DIR = Path("data/image_cache")
IMAGE_CACHE_TTL_DAYS = 7
IMAGE_MIN_SIZE_BYTES = 2_000          # 2 KB — very low, news photos are usually 50KB+
IMAGE_MAX_SIZE_BYTES = 5_242_880      # 5 MB — Telegram limit
MAX_IMAGES_PER_POST = 10              # Telegram mediagroup limit — use it!
IMAGE_FETCH_TIMEOUT = 15.0
ARTICLE_FETCH_TIMEOUT = 20.0

# ── Basic junk filter — only obvious non-content stuff ────────────────────────

JUNK_DOMAINS = {
    "mc.yandex.ru", "mc.yandex.com", "google-analytics.com",
    "facebook.com/tr", "connect.facebook.net",
    "pixel.wp.com", "stats.wordpress.com",
    "doubleclick.net", "adservice.google.com",
    "pagead2.googlesyndication.com", "ad.doubleclick.net",
    "platform.twitter.com", "apis.google.com",
}

JUNK_KEYWORDS = [
    "favicon", "avatar", "spinner", "loading", "placeholder",
    "pixel", "tracker", "beacon", "counter", "analytics",
    "1x1", "spacer", "blank", "transparent",
    "recaptcha", "captcha",
    # Common non-content images from RSS (icons, logos, social buttons)
    "icon", "logo", "badge", "button", "btn",
    "share", "facebook", "twitter", "vk.",
    "telegram", "whatsapp", "instagram", "youtube", "tiktok",
]

JUNK_EXTENSIONS = {".gif", ".svg"}


def _is_junk_url(url: str) -> bool:
    """Minimal junk filter — only block obvious tracking pixels and icons."""
    url_lower = url.lower()
    parsed = urlparse(url_lower)
    hostname = parsed.hostname or ""

    for junk in JUNK_DOMAINS:
        if junk in hostname:
            return True

    for kw in JUNK_KEYWORDS:
        if kw in url_lower:
            return True

    path = parsed.path.lower()
    for ext in JUNK_EXTENSIONS:
        if path.endswith(ext):
            return True

    return False


# ── Image Cache ───────────────────────────────────────────────────────────────

class ImageCache:
    """File-based image cache with TTL."""

    def __init__(self, cache_dir: Path = IMAGE_CACHE_DIR, ttl_days: int = IMAGE_CACHE_TTL_DAYS):
        self.cache_dir = cache_dir
        self.ttl_days = ttl_days
        self._index_path = cache_dir / "index.json"
        self._index: Dict[str, Dict[str, Any]] = {}
        self._load_index()

    def _load_index(self) -> None:
        try:
            if self._index_path.exists():
                with open(self._index_path, "r", encoding="utf-8") as f:
                    self._index = json.load(f)
        except Exception:
            self._index = {}

    def _save_index(self) -> None:
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            with open(self._index_path, "w", encoding="utf-8") as f:
                json.dump(self._index, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _cache_key(self, topic: str) -> str:
        return hashlib.md5(topic.lower().strip().encode()).hexdigest()

    def get(self, topic: str) -> Optional[List[bytes]]:
        key = self._cache_key(topic)
        entry = self._index.get(key)
        if not entry:
            return None
        age_days = (time.time() - entry.get("cached_at", 0)) / 86400
        if age_days > self.ttl_days:
            self.delete(topic)
            return None
        results = []
        for filename in entry.get("filenames", []):
            file_path = self.cache_dir / filename
            if file_path.exists():
                try:
                    with open(file_path, "rb") as f:
                        results.append(f.read())
                except Exception:
                    continue
        return results if results else None

    def put(self, topic: str, images: List[bytes], source: str) -> None:
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            key = self._cache_key(topic)
            filenames = []
            for i, img_bytes in enumerate(images):
                filename = f"{key}_{i}.jpg"
                file_path = self.cache_dir / filename
                with open(file_path, "wb") as f:
                    f.write(img_bytes)
                filenames.append(filename)
            self._index[key] = {
                "topic": topic[:100],
                "filenames": filenames,
                "source": source,
                "cached_at": time.time(),
            }
            self._save_index()
        except Exception:
            pass

    def delete(self, topic: str) -> None:
        key = self._cache_key(topic)
        entry = self._index.pop(key, None)
        if entry:
            for filename in entry.get("filenames", []):
                try:
                    file_path = self.cache_dir / filename
                    if file_path.exists():
                        file_path.unlink()
                except Exception:
                    pass
            self._save_index()


# ── Download & basic validation ───────────────────────────────────────────────

async def _download_image(client: httpx.AsyncClient, url: str) -> Optional[bytes]:
    """Download an image URL with minimal validation. Returns bytes or None."""
    if _is_junk_url(url):
        return None

    try:
        # Quick HEAD to check content-type and size
        try:
            head_resp = await client.head(url, timeout=6.0, follow_redirects=True)
            content_type = head_resp.headers.get("content-type", "").lower()
            content_length = int(head_resp.headers.get("content-length", "0"))

            # Must be an image
            if content_type and not content_type.startswith("image/"):
                return None

            if 0 < content_length < IMAGE_MIN_SIZE_BYTES:
                return None
            if content_length > IMAGE_MAX_SIZE_BYTES:
                return None
        except Exception:
            pass  # Some servers don't support HEAD, try GET anyway

        # Full GET
        resp = await client.get(url, timeout=IMAGE_FETCH_TIMEOUT, follow_redirects=True)
        if resp.status_code != 200:
            return None

        img_bytes = resp.content

        # Size check
        if len(img_bytes) < IMAGE_MIN_SIZE_BYTES or len(img_bytes) > IMAGE_MAX_SIZE_BYTES:
            return None

        # Magic bytes check — is it a real image?
        is_valid = (
            img_bytes[:3] == b'\xff\xd8\xff'       # JPEG
            or img_bytes[:4] == b'\x89PNG'           # PNG
            or img_bytes[:4] == b'RIFF'              # WebP (RIFF container)
            or img_bytes[:6] in (b'GIF87a', b'GIF89a')  # GIF
        )
        if not is_valid:
            return None

        # Skip SVG
        if b'<svg' in img_bytes[:500]:
            return None

        # Dimension check — skip tiny icons/buttons (when PIL available)
        try:
            from PIL import Image
            import io
            img = Image.open(io.BytesIO(img_bytes))
            w, h = img.size
            # Skip icons, thumbnails, buttons — real article photos are at least 200x150
            if w < 200 or h < 150:
                return None
            # Skip banners (extreme aspect ratios)
            if w / max(h, 1) > 4.0 or h / max(w, 1) > 4.0:
                return None
        except ImportError:
            pass  # PIL not available — accept without dimension check
        except Exception:
            pass  # Can't read dimensions — accept anyway

        return img_bytes

    except Exception:
        return None


# ── Strategy 1: RSS image extraction ─────────────────────────────────────────

def extract_rss_images(entry: Any) -> List[str]:
    """Extract image URLs from a feedparser entry.

    Checks: media_content, enclosures, media_thumbnail, links, <img> in content.
    Returns up to 10 image URLs.
    """
    images = []
    seen = set()

    def _add(url: str):
        if not url or len(url) < 15:
            return
        url = url.strip()
        if url.startswith("//"):
            url = "https:" + url
        if url not in seen and not _is_junk_url(url):
            seen.add(url)
            images.append(url)

    # media:content (Media RSS — most reliable for article photos)
    for mc in getattr(entry, "media_content", []) or []:
        url = mc.get("url", "")
        mc_type = mc.get("type", "").lower()
        medium = mc.get("medium", "").lower()
        if url and (medium == "image" or "image" in mc_type or _has_image_ext(url)):
            _add(url)

    # enclosures
    for enc in getattr(entry, "enclosures", []) or []:
        url = enc.get("href", "") or enc.get("url", "")
        enc_type = enc.get("type", "").lower()
        if url and ("image" in enc_type or _has_image_ext(url)):
            _add(url)

    # media:thumbnail
    for mt in getattr(entry, "media_thumbnail", []) or []:
        url = mt.get("url", "")
        if url:
            _add(url)

    # links with image type
    for link in getattr(entry, "links", []) or []:
        link_type = link.get("type", "").lower()
        link_rel = link.get("rel", "").lower()
        url = link.get("href", "")
        if url and ("image" in link_type or link_rel == "enclosure"):
            _add(url)

    # <img> tags in content/summary/description
    for field_name in ("content", "summary", "description"):
        content_value = getattr(entry, field_name, None)
        if isinstance(content_value, list):
            content_value = content_value[0].get("value", "") if content_value else ""
        elif content_value is None:
            continue
        for m in re.finditer(r'<img[^>]+src=["\']([^"\']+)["\']', str(content_value), re.IGNORECASE):
            url = m.group(1).replace("&amp;", "&")
            _add(url)

    return images[:10]


def _has_image_ext(url: str) -> bool:
    try:
        path = urlparse(url).path.lower().split("?")[0].split("#")[0]
        return any(path.endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".webp"])
    except Exception:
        return False


# ── Strategy 2: Article page image extraction ────────────────────────────────

async def fetch_article_images(url: str, client: httpx.AsyncClient, max_count: int = 10) -> List[Tuple[bytes, str]]:
    """Fetch images from an article page. Returns list of (image_bytes, url).

    Extracts in priority order:
    1. og:image / og:image:url / og:image:secure_url
    2. twitter:image
    3. JSON-LD structured data
    4. <picture>/<source srcset>
    5. <img> tags (data-src first for lazy-loaded, then src)
    """
    results: List[Tuple[bytes, str]] = []

    if not url or not url.startswith(("http://", "https://")):
        return results

    try:
        _SCRAPE_HEADERS = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5,ru-RU;q=0.8",
        }

        resp = await client.get(url, headers=_SCRAPE_HEADERS, timeout=ARTICLE_FETCH_TIMEOUT)
        if resp.status_code != 200:
            return results

        html = resp.text

        # Collect candidate URLs in priority order
        candidate_urls: List[str] = []
        seen: set = set()

        # 1. og:image
        for pattern in [
            r'<meta[^>]+property=["\x27]og:image["\x27][^>]+content=["\x27]([^"\x27]+)["\x27]',
            r'<meta[^>]+content=["\x27]([^"\x27]+)["\x27][^>]+property=["\x27]og:image["\x27]',
            r'<meta[^>]+property=["\x27]og:image:url["\x27][^>]+content=["\x27]([^"\x27]+)["\x27]',
            r'<meta[^>]+property=["\x27]og:image:secure_url["\x27][^>]+content=["\x27]([^"\x27]+)["\x27]',
        ]:
            for m in re.finditer(pattern, html, re.IGNORECASE):
                u = m.group(1).replace("&amp;", "&")
                if u and u not in seen and not _is_junk_url(u):
                    seen.add(u)
                    candidate_urls.append(u)

        # 2. twitter:image
        for pattern in [
            r'<meta[^>]+name=["\x27]twitter:image["\x27][^>]+content=["\x27]([^"\x27]+)["\x27]',
            r'<meta[^>]+content=["\x27]([^"\x27]+)["\x27][^>]+name=["\x27]twitter:image["\x27]',
        ]:
            for m in re.finditer(pattern, html, re.IGNORECASE):
                u = m.group(1).replace("&amp;", "&")
                if u and u not in seen and not _is_junk_url(u):
                    seen.add(u)
                    candidate_urls.append(u)

        # 3. JSON-LD
        jsonld_urls = _extract_jsonld_images(html)
        for u in jsonld_urls:
            if u not in seen and not _is_junk_url(u):
                seen.add(u)
                candidate_urls.append(u)

        # 4. <picture>/<source srcset>
        picture_blocks = re.findall(r'<picture[^>]*>(.*?)</picture>', html, re.IGNORECASE | re.DOTALL)
        for block in picture_blocks:
            srcsets = re.findall(r'srcset=["\x27]([^"\x27]+)["\x27]', block, re.IGNORECASE)
            for srcset in srcsets:
                for part in srcset.split(','):
                    u = part.strip().split()[0] if part.strip() else ''
                    if u and u not in seen and not _is_junk_url(u):
                        seen.add(u)
                        candidate_urls.append(u)

        # 5. <img> tags from article body
        article_html = ""
        for pattern in [
            r'<article[^>]*>(.*?)</article>',
            r'<main[^>]*>(.*?)</main>',
            r'<div[^>]+class=["\x27][^"\x27]*(?:content|article|post|entry)[^"\x27]*["\x27][^>]*>(.*?)</div>',
        ]:
            matches = re.findall(pattern, html, re.IGNORECASE | re.DOTALL)
            for match in matches:
                article_html += match + "\n"

        search_html = article_html if article_html else html

        # Lazy-loaded first (usually higher quality originals)
        for attr_pattern in [
            r'<img[^>]+data-src=["\x27]([^"\x27]+)["\x27]',
            r'<img[^>]+data-lazy-src=["\x27]([^"\x27]+)["\x27]',
            r'<img[^>]+src=["\x27]([^"\x27]+)["\x27]',
        ]:
            for m in re.finditer(attr_pattern, search_html, re.IGNORECASE):
                u = m.group(1).replace("&amp;", "&")
                if u.startswith("//"):
                    u = "https:" + u
                if u and len(u) > 10 and u not in seen and not _is_junk_url(u):
                    seen.add(u)
                    candidate_urls.append(u)

        logger.info(f"Scraped {len(candidate_urls)} candidate image URLs from {url[:60]}")

        # Download candidates
        for img_url in candidate_urls[:max_count * 3]:
            if len(results) >= max_count:
                break
            img_bytes = await _download_image(client, img_url)
            if img_bytes:
                results.append((img_bytes, img_url))
                logger.info(f"Downloaded article image: {img_url[:80]} ({len(img_bytes)} bytes)")

    except Exception as e:
        logger.debug(f"Article image extraction failed for {url[:60]}: {e}")

    return results


def _extract_jsonld_images(html: str) -> List[str]:
    """Extract image URLs from JSON-LD structured data."""
    images = []
    try:
        jsonld_blocks = re.findall(
            r'<script[^>]+type=["\x27]application/ld\+json["\x27][^>]*>(.*?)</script>',
            html, re.IGNORECASE | re.DOTALL,
        )
        for block in jsonld_blocks:
            try:
                data = json.loads(block)
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    img_field = item.get("image") or item.get("images")
                    if not img_field:
                        continue
                    if isinstance(img_field, str):
                        images.append(img_field)
                    elif isinstance(img_field, dict):
                        url = img_field.get("url") or img_field.get("contentUrl") or img_field.get("@id", "")
                        if url:
                            images.append(url)
                    elif isinstance(img_field, list):
                        for img_item in img_field:
                            if isinstance(img_item, str):
                                images.append(img_item)
                            elif isinstance(img_item, dict):
                                url = img_item.get("url") or img_item.get("contentUrl") or img_item.get("@id", "")
                                if url:
                                    images.append(url)
            except Exception:
                continue
    except Exception:
        pass
    return images


# ── Deduplication ─────────────────────────────────────────────────────────────

def _image_hash(img_bytes: bytes) -> str:
    return hashlib.sha256(img_bytes).hexdigest()


def deduplicate_images(images: List[bytes]) -> List[bytes]:
    """Remove exact duplicates by SHA256 hash."""
    if not images:
        return images
    seen: set = set()
    unique: List[bytes] = []
    for img in images:
        h = _image_hash(img)
        if h not in seen:
            seen.add(h)
            unique.append(img)
    removed = len(images) - len(unique)
    if removed > 0:
        logger.info(f"Image dedup: {len(images)} -> {len(unique)} (removed {removed} duplicates)")
    return unique


# ── Main fetcher ──────────────────────────────────────────────────────────────

class ImageFetcher:
    """Simple image fetcher — article photos only, no search, no AI.

    Usage:
        fetcher = ImageFetcher()
        images, source = await fetcher.fetch(
            topic="BMW M5 G90 debut",
            article_url="https://bmwblog.com/...",
            rss_entry=feed_entry,
        )
        # images = [bytes, bytes, ...] up to 10 photos
    """

    def __init__(self) -> None:
        self.cache = ImageCache()
        self._client: Optional[httpx.AsyncClient] = None
        self._seen_hashes: set = set()

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=IMAGE_FETCH_TIMEOUT,
                follow_redirects=True,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                    "Accept": "image/webp,image/apng,image/*,*/*;q=0.8",
                },
            )
        return self._client

    async def fetch(
        self,
        topic: str,
        article_url: str = "",
        rss_entry: Any = None,
        image_urls: List[str] = None,
        max_images: int = MAX_IMAGES_PER_POST,
    ) -> Tuple[List[bytes], str]:
        """Fetch images from the article. Simple 2-step pipeline.

        Step 1: RSS images (media:content, enclosures, <img> in content)
        Step 2: Article page scraping (og:image, JSON-LD, <img> tags)

        Returns (image_list, source_str).
        """
        self._seen_hashes = set()
        client = self._get_client()
        all_images: List[bytes] = []
        source = "none"

        # ── Check cache first ──────────────────────────────────────────
        cached = self.cache.get(topic)
        if cached:
            cached = deduplicate_images(cached)[:max_images]
            if cached:
                logger.info(f"Image cache HIT for '{topic[:50]}' — {len(cached)} images")
                return cached, "cache"

        # ── Step 1: RSS images ─────────────────────────────────────────
        # image_urls = already extracted by news._extract_entry_images()
        rss_urls = list(image_urls or [])

        # Also extract from raw RSS entry if provided
        if rss_entry is not None:
            entry_urls = extract_rss_images(rss_entry)
            for u in entry_urls:
                if u not in rss_urls:
                    rss_urls.append(u)

        if rss_urls:
            for url in rss_urls[:max_images * 2]:
                if len(all_images) >= max_images:
                    break
                img_bytes = await _download_image(client, url)
                if img_bytes:
                    h = _image_hash(img_bytes)
                    if h not in self._seen_hashes:
                        self._seen_hashes.add(h)
                        all_images.append(img_bytes)
            if all_images:
                source = "rss"
                logger.info(f"Got {len(all_images)} images from RSS for '{topic[:50]}'")

        # ── Step 2: Article page ──────────────────────────────────────
        if article_url and len(all_images) < max_images:
            article_results = await fetch_article_images(
                article_url, client,
                max_count=max_images - len(all_images) + 2,
            )
            for img_bytes, img_url in article_results:
                if len(all_images) >= max_images:
                    break
                h = _image_hash(img_bytes)
                if h not in self._seen_hashes:
                    self._seen_hashes.add(h)
                    all_images.append(img_bytes)
            if article_results and source == "none":
                source = "article"
            elif article_results:
                source += "+article"

        # ── Dedup & cache ──────────────────────────────────────────────
        all_images = deduplicate_images(all_images)[:max_images]

        if all_images:
            self.cache.put(topic, all_images, source=source)
            logger.info(f"ImageFetcher: {len(all_images)} images for '{topic[:50]}' (source={source})")
        else:
            logger.info(f"No images found for '{topic[:50]}' — post will be text-only")

        return all_images, source

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None


# ── Module-level convenience ─────────────────────────────────────────────────

_fetcher: Optional[ImageFetcher] = None


async def fetch_images_for_post(
    topic: str,
    article_url: str = "",
    rss_entry: Any = None,
    image_urls: List[str] = None,
    max_images: int = MAX_IMAGES_PER_POST,
) -> Tuple[List[bytes], str]:
    """Fetch images for a post. Up to 10 photos from article/RSS."""
    global _fetcher
    if _fetcher is None:
        _fetcher = ImageFetcher()
    return await _fetcher.fetch(
        topic=topic,
        article_url=article_url,
        rss_entry=rss_entry,
        image_urls=image_urls,
        max_images=max_images,
    )
