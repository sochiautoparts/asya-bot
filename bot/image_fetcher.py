"""Smart Image Fetcher v3.0 — Original-first image sourcing for asya-bot.

PRIORITY PIPELINE:
  1. Article images — og:image / twitter:image / JSON-LD / <img> from source URL
  2. RSS enclosures — <enclosure> / <media:content> from RSS feed
  3. Image search — SearXNG images / web search
  4. AI generation — Pollinations (LAST RESORT ONLY, handled by caller)

KEY FEATURES:
  - Extracts original photos from article pages (og:image, twitter:image, JSON-LD)
  - Parses RSS enclosures and media:content (using news._extract_entry_images)
  - Validates images: min size, content-type, dimensions (PIL when available)
  - **DEDUPLICATES images by SHA256 hash** — no duplicate photos in a post!
  - Caches images by topic/entity with 7-day TTL
  - Blacklist of junk image domains and patterns
  - Returns image bytes ready for Telegram (compatible with channel.py)

NOTE: This module returns image BYTES (not base64) because that's what
channel.py expects for Telegram posting.
"""

from __future__ import annotations

import asyncio
import base64
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
IMAGE_MIN_SIZE_BYTES = 3_000         # 3 KB — lower threshold for news images
IMAGE_MAX_SIZE_BYTES = 5_242_880     # 5 MB — matching channel.py limit
IMAGE_MIN_WIDTH = 320
IMAGE_MIN_HEIGHT = 240
IMAGE_FETCH_TIMEOUT = 15.0
ARTICLE_FETCH_TIMEOUT = 20.0
MAX_IMAGES_PER_SOURCE = 10           # Telegram mediagroup limit
MAX_IMAGES_PER_POST = 4              # Optimal for news posts (2-4 unique images)
IMAGE_HASH_THRESHOLD = 0.95          # Similarity threshold for near-duplicate detection

# ── Blacklist — junk image URLs that should never be used ─────────────────────

JUNK_DOMAINS = {
    "pixel", "tracker", "analytics", "counter", "beacon",
    "mc.yandex.ru", "mc.yandex.com", "google-analytics.com",
    "facebook.com/tr", "connect.facebook.net",
    "feeds.feedburner.com", "feedburner.google.com",
    "pixel.wp.com", "stats.wordpress.com",
    "doubleclick.net", "adservice.google.com",
    "pagead2.googlesyndication.com", "ad.doubleclick.net",
    "platform.twitter.com", "apis.google.com",
}

JUNK_PATTERNS = [
    r"[\?&](utm_|ref|share|action|callback|client_id)=.*$",
    r"/(icon|logo|favicon|badge|avatar|spinner|loading|placeholder|blank|pixel)\b",
    r"\d+x\d+\.(gif|png)$",
    r"tracker|beacon|pixel|counter|analytics",
    r"gravatar|avatar|profile.*photo",
    r"(button|btn|icon|logo|badge|spinner)\.(png|gif|svg|webp)$",
]

# These keywords match channel.py _is_junk_image_url — keeping consistent
JUNK_KEYWORDS = [
    "icon", "logo", "favicon", "avatar", "badge", "button", "btn",
    "spinner", "loading", "placeholder", "pixel", "tracker",
    "analytics", "share", "facebook", "twitter", "vk.",
    "telegram", "whatsapp", "instagram", "youtube", "tiktok",
    # NOTE: "ads/" removed — was blocking /uploads/ in WordPress URLs!
    # Use JUNK_DOMAINS for ad-specific domains instead.
    "advert", "sponsor", "ad_banner", "ad_image",
    "emoji", "smileys", "captcha", "recaptcha",
    "1x1", "spacer", "blank", "transparent", "dot.",
    # NOTE: "watermark" removed — many real news photos have watermarks
    # It's better to use a real photo with a watermark than no photo at all.
]

JUNK_EXTENSIONS = {".gif", ".svg"}


# ── Image Cache ───────────────────────────────────────────────────────────────

class ImageCache:
    """File-based image cache with TTL. Key = entity/topic, Value = image data."""

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
        except Exception as e:
            logger.debug(f"Failed to load image cache index: {e}")
            self._index = {}

    def _save_index(self) -> None:
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            with open(self._index_path, "w", encoding="utf-8") as f:
                json.dump(self._index, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.debug(f"Failed to save image cache index: {e}")

    def _cache_key(self, topic: str) -> str:
        return hashlib.md5(topic.lower().strip().encode()).hexdigest()

    def get(self, topic: str) -> Optional[List[bytes]]:
        """Get cached image bytes for a topic."""
        key = self._cache_key(topic)
        entry = self._index.get(key)
        if not entry:
            return None

        cached_at = entry.get("cached_at", 0)
        age_days = (time.time() - cached_at) / 86400
        if age_days > self.ttl_days:
            self.delete(topic)
            return None

        # Load all cached files for this topic
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
        """Store images in cache."""
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
        except Exception as e:
            logger.debug(f"Failed to cache images: {e}")

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


# ── Validation ────────────────────────────────────────────────────────────────

def _is_junk_url(url: str) -> bool:
    """Check if a URL is a junk/tracking/icon image.
    
    Consistent with channel.py _is_junk_image_url but enhanced.
    """
    url_lower = url.lower()
    parsed = urlparse(url_lower)
    hostname = parsed.hostname or ""

    # Check junk domains
    for junk in JUNK_DOMAINS:
        if junk in hostname:
            return True

    # Check junk keywords (from channel.py)
    for kw in JUNK_KEYWORDS:
        if kw in url_lower:
            return True

    # Check junk patterns
    for pattern in JUNK_PATTERNS:
        if re.search(pattern, url_lower):
            return True

    # Check junk extensions
    path = parsed.path.lower()
    for ext in JUNK_EXTENSIONS:
        if path.endswith(ext):
            return True

    # Skip URLs with very small size indicators
    # NOTE: h=0 means "auto height" (proportional) — common in image CDNs
    # like TopSpeed (_800x0.webp = width 800, height auto). Do NOT block these.
    size_pattern = re.compile(r'[/=_x](\d{1,4})x(\d{1,4})[/._]')
    size_match = size_pattern.search(url_lower)
    if size_match:
        w, h = int(size_match.group(1)), int(size_match.group(2))
        # 0 = auto/proportional height — valid image size indicator
        if w < 100 or (0 < h < 100):
            return True

    return False


def _is_content_image(image_data: bytes) -> bool:
    """Validate that image data represents a proper content photo.
    
    Uses PIL when available (checks dimensions, aspect ratio).
    Falls back to soft check when PIL is not available.
    """
    if len(image_data) < IMAGE_MIN_SIZE_BYTES:
        return False

    try:
        from PIL import Image
        import io

        img = Image.open(io.BytesIO(image_data))
        width, height = img.size

        if width < IMAGE_MIN_WIDTH or height < IMAGE_MIN_HEIGHT:
            return False
        if width / max(height, 1) > 3.0:
            return False
        if height / max(width, 1) > 3.0:
            return False
        if width * height < 76800:  # 320*240 = 76800
            return False

        return True
    except ImportError:
        logger.warning("PIL not available, accepting image without dimension check")
        return True
    except Exception:
        return True  # Soft check — accept if can't read


async def _validate_and_download(
    client: httpx.AsyncClient,
    url: str,
) -> Optional[bytes]:
    """Download and validate an image URL. Returns image bytes or None."""
    if _is_junk_url(url):
        return None

    try:
        # Quick HEAD check
        try:
            head_resp = await client.head(url, timeout=6.0, follow_redirects=True)
            content_type = head_resp.headers.get("content-type", "").lower()
            content_length = int(head_resp.headers.get("content-length", "0"))

            if content_type and not any(ct in content_type for ct in [
                "image/jpeg", "image/png", "image/webp", "image/jpg",
            ]):
                # Some servers don't return proper content-type on HEAD
                if not content_type.startswith("image/"):
                    return None

            if 0 < content_length < IMAGE_MIN_SIZE_BYTES:
                return None
            if content_length > IMAGE_MAX_SIZE_BYTES:
                return None
        except Exception:
            pass

        # Full GET
        resp = await client.get(url, timeout=IMAGE_FETCH_TIMEOUT, follow_redirects=True)
        if resp.status_code != 200:
            return None

        img_bytes = resp.content

        if len(img_bytes) < IMAGE_MIN_SIZE_BYTES:
            return None
        if len(img_bytes) > IMAGE_MAX_SIZE_BYTES:
            return None

        # Content-type check
        content_type = resp.headers.get("content-type", "").lower()
        if content_type and not any(ct in content_type for ct in [
            "image/jpeg", "image/png", "image/webp", "image/jpg",
            "application/octet-stream",
        ]):
            return None

        # Skip SVG
        if b'<svg' in img_bytes[:500] or 'svg' in content_type:
            return None

        # Magic bytes check
        is_valid_format = (
            img_bytes[:3] == b'\xff\xd8\xff'  # JPEG
            or img_bytes[:4] == b'\x89PNG'     # PNG
            or img_bytes[:4] == b'RIFF'        # WebP
            or img_bytes[:6] in (b'GIF87a', b'GIF89a')  # GIF
        )
        if not is_valid_format:
            return None

        # Dimension check (PIL when available)
        if not _is_content_image(img_bytes):
            return None

        return img_bytes

    except Exception as e:
        logger.debug(f"Image download failed for {url[:60]}: {e}")
        return None


# ── Strategy 1: Article page image extraction ────────────────────────────────

async def fetch_article_images(url: str, max_count: int = 10) -> List[bytes]:
    """Fetch original images from an article page.

    Extracts images in priority order:
    1. og:image / og:image:url / og:image:secure_url
    2. twitter:image
    3. JSON-LD structured data (schema.org image field)
    4. <picture>/<source srcset> elements
    5. <img> tags from article body (src + data-src for lazy loading)
    """
    images: List[bytes] = []

    if not url or not url.startswith(("http://", "https://")):
        return images

    try:
        _SCRAPE_HEADERS = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }

        async with httpx.AsyncClient(
            timeout=ARTICLE_FETCH_TIMEOUT,
            follow_redirects=True,
            max_redirects=5,
        ) as client:
            resp = await client.get(url, headers=_SCRAPE_HEADERS)
            if resp.status_code != 200:
                return images

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

            # 3. JSON-LD structured data (schema.org)
            jsonld_urls = _extract_jsonld_images(html)
            for u in jsonld_urls:
                if u not in seen and not _is_junk_url(u):
                    seen.add(u)
                    candidate_urls.append(u)

            # 4. <picture>/<source srcset> elements
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

            # Lazy-loaded images first (often higher quality)
            lazy_urls = re.findall(r'<img[^>]+data-src=["\x27]([^"\x27]+)["\x27]', search_html, re.IGNORECASE)
            lazy_urls += re.findall(r'<img[^>]+data-lazy-src=["\x27]([^"\x27]+)["\x27]', search_html, re.IGNORECASE)
            regular_urls = re.findall(r'<img[^>]+src=["\x27]([^"\x27]+)["\x27]', search_html, re.IGNORECASE)

            for u in lazy_urls + regular_urls:
                if u.startswith("//"):
                    u = "https:" + u
                if u and len(u) > 10 and u not in seen and not _is_junk_url(u):
                    seen.add(u)
                    candidate_urls.append(u)

            logger.info(f"Scraped {len(candidate_urls)} candidate image URLs from {url[:60]}")

            # Download and validate
            for img_url in candidate_urls[:max_count * 3]:
                if len(images) >= max_count:
                    break
                img_bytes = await _validate_and_download(client, img_url)
                if img_bytes:
                    images.append(img_bytes)
                    logger.info(f"Downloaded article image: {img_url[:80]} ({len(img_bytes)} bytes)")

    except Exception as e:
        logger.debug(f"Article image extraction failed for {url[:60]}: {e}")

    return images


def _extract_jsonld_images(html: str) -> List[str]:
    """Extract image URLs from JSON-LD structured data in HTML."""
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


# ── Strategy 2: RSS enclosure / media:content extraction ─────────────────────

def extract_rss_images(entry: Any) -> List[str]:
    """Extract image URLs from a feedparser entry.
    
    Delegates to news._extract_entry_images for consistency with existing code.
    """
    try:
        from news import _extract_entry_images
        return _extract_entry_images(entry)
    except ImportError:
        # Fallback if news module not available
        return _fallback_extract_rss_images(entry)


def _fallback_extract_rss_images(entry: Any) -> List[str]:
    """Fallback RSS image extraction."""
    image_urls: List[str] = []

    # enclosures
    for enc in getattr(entry, "enclosures", []) or []:
        url = enc.get("href", "") or enc.get("url", "")
        enc_type = enc.get("type", "").lower()
        if url and ("image" in enc_type or any(ext in url.lower() for ext in [".jpg", ".jpeg", ".png", ".webp"])):
            if not _is_junk_url(url) and url not in image_urls:
                image_urls.append(url)

    # media:content
    for mc in getattr(entry, "media_content", []) or []:
        url = mc.get("url", "")
        medium = mc.get("medium", "").lower()
        mc_type = mc.get("type", "").lower()
        if url and (medium == "image" or "image" in mc_type):
            if not _is_junk_url(url) and url not in image_urls:
                image_urls.append(url)

    # media:thumbnail
    for mt in getattr(entry, "media_thumbnail", []) or []:
        url = mt.get("url", "")
        if url and not _is_junk_url(url) and url not in image_urls:
            image_urls.append(url)

    # <img> in content/summary
    for field_name in ("content", "summary", "description"):
        content_value = getattr(entry, field_name, None)
        if isinstance(content_value, list):
            content_value = content_value[0].get("value", "") if content_value else ""
        elif content_value is None:
            continue
        for m in re.finditer(r'<img[^>]+src=["\x27]([^"\x27]+)["\x27]', str(content_value), re.IGNORECASE):
            url = m.group(1).replace("&amp;", "&")
            if not _is_junk_url(url) and url not in image_urls:
                image_urls.append(url)

    return image_urls[:10]


# ── Strategy 3: Image search ─────────────────────────────────────────────────

async def search_images(topic: str, max_images: int = 5) -> List[str]:
    """Search for images related to a topic using SearXNG image search.
    
    v3.0: Smarter search queries — adapts to automotive topics.
    Tries multiple search strategies for better image coverage.
    """
    image_urls: List[str] = []
    seen_urls: set = set()

    try:
        from bot.web_search import search_searxng

        clean_topic = re.sub(r'[^\w\s]', '', topic)[:80]

        # Build smarter search queries based on topic type
        # Not all news is about cars — adapt the query
        search_queries = []
        
        # Primary: topic + photo (works for any news)
        search_queries.append(f"{clean_topic} фото")
        
        # Secondary: English query for international sources
        search_queries.append(f"{clean_topic} photo")
        
        # Try SearXNG image search with each query
        for query in search_queries:
            if len(image_urls) >= max_images:
                break
            try:
                results = await search_searxng(
                    query,
                    max_results=8,
                    language="ru",
                    categories="images",
                )
                for r in results:
                    if r.url and r.url not in seen_urls:
                        url_lower = r.url.lower()
                        # Accept image URLs
                        is_image_url = any(ext in url_lower for ext in ['.jpg', '.jpeg', '.png', '.webp'])
                        is_image_host = any(domain in url_lower for domain in [
                            'imgur.com', 'flickr.com', 'unsplash.com',
                            'pexels.com', 'shutterstock.com', 'istockphoto.com',
                        ])
                        if (is_image_url or is_image_host) and not _is_junk_url(r.url):
                            seen_urls.add(r.url)
                            image_urls.append(r.url)
            except Exception as e:
                logger.debug(f"SearXNG image search failed for query '{query[:40]}': {e}")

        # Fallback: regular web search for images
        if not image_urls:
            try:
                from bot.web_search import web_search
                results = await web_search(f"{clean_topic} фото image", max_results=5)
                for r in results:
                    url = r.get("url", "") if isinstance(r, dict) else getattr(r, "url", "")
                    if url and url not in seen_urls and not _is_junk_url(url):
                        seen_urls.add(url)
                        image_urls.append(url)
            except Exception as e:
                logger.debug(f"Web search image fallback failed for '{topic[:40]}': {e}")

    except Exception as e:
        logger.debug(f"Image search failed for '{topic}': {e}")

    logger.info(f"Image search found {len(image_urls)} URLs for '{topic[:50]}'")
    return image_urls[:max_images]


# ── Main fetcher class ───────────────────────────────────────────────────────

def _image_hash(img_bytes: bytes) -> str:
    """Compute SHA256 hash of image bytes for deduplication."""
    return hashlib.sha256(img_bytes).hexdigest()


def _images_are_similar(img1: bytes, img2: bytes) -> bool:
    """Check if two images are identical or near-identical.

    Uses SHA256 hash for exact match. For near-duplicates (resized/compressed
    versions of the same photo), uses a simple pixel sampling heuristic.
    Returns True if images are likely the same photo.
    """
    # Exact match — fastest check
    if img1 == img2:
        return True

    h1, h2 = _image_hash(img1), _image_hash(img2)
    if h1 == h2:
        return True

    # Size-based quick reject: if sizes differ by >3x, very unlikely same image
    ratio = len(img1) / max(len(img2), 1)
    if ratio < 0.3 or ratio > 3.3:
        return False

    # Try PIL-based comparison for near-duplicates (resized/recompressed same photo)
    try:
        from PIL import Image
        import io

        pil1 = Image.open(io.BytesIO(img1))
        pil2 = Image.open(io.BytesIO(img2))

        # If dimensions are very different, probably different images
        w1, h1_dim = pil1.size
        w2, h2_dim = pil2.size
        if abs(w1 - w2) > max(w1, w2) * 0.3 or abs(h1_dim - h2_dim) > max(h1_dim, h2_dim) * 0.3:
            return False

        # Resize both to tiny thumbnail and compare pixel values
        thumb_size = (16, 16)
        t1 = pil1.convert("L").resize(thumb_size)
        t2 = pil2.convert("L").resize(thumb_size)

        pixels1 = list(t1.getdata())
        pixels2 = list(t2.getdata())

        # Compare average pixel difference
        total_diff = sum(abs(p1 - p2) for p1, p2 in zip(pixels1, pixels2))
        avg_diff = total_diff / len(pixels1)

        # Low average difference = same image with different compression/resize
        if avg_diff < 15:  # Threshold: very similar greyscale thumbnails
            return True

    except Exception:
        pass  # If PIL comparison fails, only exact hash match counts

    return False


def deduplicate_images(images: List[bytes]) -> List[bytes]:
    """Remove duplicate and near-duplicate images from a list.

    Uses both exact SHA256 hash matching and PIL-based perceptual comparison
    to catch resized/compressed versions of the same photo.
    Returns deduplicated list preserving original order.
    """
    if not images:
        return images

    unique: List[bytes] = []
    for img in images:
        is_dup = False
        for existing in unique:
            if _images_are_similar(img, existing):
                is_dup = True
                break
        if not is_dup:
            unique.append(img)
        else:
            logger.debug(f"Dedup: removed duplicate image ({len(img)} bytes)")

    removed = len(images) - len(unique)
    if removed > 0:
        logger.info(f"Image dedup: {len(images)} → {len(unique)} (removed {removed} duplicates)")

    return unique


class ImageFetcher:
    """Smart image fetcher with original-first priority pipeline.

    Returns image BYTES (not base64) for compatibility with channel.py.

    v3.0: Includes image deduplication by hash + perceptual comparison
    to prevent duplicate photos in Telegram posts.

    Usage:
        fetcher = ImageFetcher()
        images = await fetcher.fetch(
            topic="BMW M5 G90 debut",
            article_url="https://bmwblog.com/...",
            rss_entry=feed_entry,
        )
        # images = [bytes, bytes, ...] or [] (deduplicated!)
    """

    def __init__(self) -> None:
        self.cache = ImageCache()
        self._client: Optional[httpx.AsyncClient] = None
        self._seen_hashes: set = set()  # Track hashes within a single fetch() call

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=IMAGE_FETCH_TIMEOUT,
                follow_redirects=True,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                    "Accept": "image/webp,image/apng,image/*,*/*;q=0.8",
                },
            )
        return self._client

    def _add_unique(self, img_bytes: bytes) -> bool:
        """Add image to list only if it's not a duplicate. Returns True if added."""
        img_hash = _image_hash(img_bytes)
        if img_hash in self._seen_hashes:
            logger.debug(f"Skipping exact duplicate image (hash={img_hash[:12]}...)")
            return False
        self._seen_hashes.add(img_hash)
        return True

    async def fetch(
        self,
        topic: str,
        article_url: str = "",
        rss_entry: Any = None,
        image_urls: List[str] = None,
        max_images: int = MAX_IMAGES_PER_POST,
    ) -> Tuple[List[bytes], str]:
        """Fetch images using the priority pipeline with deduplication.

        Returns (image_list: List[bytes], source: str)
        source is 'rss', 'article', 'search', or 'cache' for logging.
        Images are deduplicated by hash to prevent duplicates in posts.
        """
        # Reset seen hashes for this fetch call
        self._seen_hashes = set()

        # ── Step 0: Check cache ───────────────────────────────────────────
        cached = self.cache.get(topic)
        if cached:
            # Deduplicate cached images too (cache may have been polluted)
            deduped = deduplicate_images(cached)
            if deduped:
                logger.info(f"Image cache HIT for '{topic[:50]}' — {len(deduped)} images (after dedup)")
                return deduped, "cache"
            else:
                logger.warning(f"Image cache had only duplicates for '{topic[:50]}', re-fetching")
                self.cache.delete(topic)

        all_images: List[bytes] = []
        source = "none"

        # ── Step 1: RSS image URLs (passed from news._extract_entry_images) ─
        if image_urls:
            client = self._get_client()
            for url in image_urls[:max_images * 3]:
                if len(all_images) >= max_images:
                    break
                img_bytes = await _validate_and_download(client, url)
                if img_bytes and self._add_unique(img_bytes):
                    all_images.append(img_bytes)
            if all_images:
                source = "rss"
                logger.info(f"Got {len(all_images)} unique images from RSS for '{topic[:50]}'")

        # ── Step 2: RSS entry enclosures (if entry provided) ──────────────
        if rss_entry is not None and len(all_images) < max_images:
            entry_urls = extract_rss_images(rss_entry)
            if entry_urls:
                client = self._get_client()
                for url in entry_urls:
                    if len(all_images) >= max_images:
                        break
                    if url not in (image_urls or []):
                        img_bytes = await _validate_and_download(client, url)
                        if img_bytes and self._add_unique(img_bytes):
                            all_images.append(img_bytes)
                if all_images and source == "none":
                    source = "rss"

        # ── Step 3: Article page images ───────────────────────────────────
        if article_url and len(all_images) < max_images:
            article_images = await fetch_article_images(article_url, max_count=max_images - len(all_images) + 2)
            if article_images:
                added = 0
                for img in article_images:
                    if len(all_images) >= max_images:
                        break
                    if self._add_unique(img):
                        all_images.append(img)
                        added += 1
                if added:
                    source = "article" if source == "none" else source + "+article"
                    logger.info(f"Scraped {added} unique article images for '{topic[:50]}'")

        # ── Step 4: Image search (if we still need more images) ───────────
        if len(all_images) < 2:
            search_urls = await search_images(topic, max_images=max(3, max_images - len(all_images)))
            if search_urls:
                client = self._get_client()
                for url in search_urls:
                    if len(all_images) >= max_images:
                        break
                    img_bytes = await _validate_and_download(client, url)
                    if img_bytes and self._add_unique(img_bytes):
                        all_images.append(img_bytes)
                if all_images and source == "none":
                    source = "search"
                elif all_images:
                    source += "+search"

        # ── Final deduplication pass (catches near-duplicates across sources) ──
        all_images = deduplicate_images(all_images)

        # ── Cache result ──────────────────────────────────────────────────
        if all_images:
            self.cache.put(topic, all_images, source=source)
            logger.info(f"ImageFetcher: {len(all_images)} unique images for '{topic[:50]}' (source={source})")
        else:
            logger.info(f"No real images found for '{topic[:50]}' — AI generation will be fallback")

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
    """Module-level convenience function to fetch images for a post.

    Images are automatically deduplicated by hash to prevent duplicates.
    Default max_images=4 (optimal for news posts — 2-4 unique images).
    """
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
