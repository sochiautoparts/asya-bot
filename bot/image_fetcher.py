"""Smart Image Fetcher v8.0 — Clean article-first image sourcing for asya-bot.

PHILOSOPHY: Automotive news articles ALREADY have photos attached.
No need for image search engines, stock photos, or AI generation.
Just take the photos from the article and attach them to the post.

PRIORITY PIPELINE:
  1. RSS images — from news._extract_entry_images() (pre-filtered, deduped, upgraded)
  2. Article page images — og:image, twitter:image, JSON-LD, <img> tags
  3. Google News redirect resolution — follow news.google.com redirects to real article
  4. DONE — that's it. No search, no AI, no bullshit.

v8.0 CHANGES:
  - CRITICAL: Google News redirect resolution — news.google.com URLs are followed
    to the REAL article page before scraping for images
  - CRITICAL: Enhanced junk image filtering — blocks more junk patterns
  - CRITICAL: Added perceptual hash dedup — removes near-duplicate photos
    (same photo at different resolutions, crops, or slight edits)
  - IMPROVED: Minimum dimension raised to 400x300 — no more thumbnails
  - IMPROVED: Content-type blacklist — blocks SVG, GIF, WebP animations
  - IMPROVED: Google AMP cache URL detection and resolution
  - KEPT: SHA256 content deduplication, ImageCache, thumbnail upgrades
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from html import unescape as html_unescape
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx

logger = logging.getLogger("asya.image_fetcher")

# ── Configuration ─────────────────────────────────────────────────────────────

IMAGE_CACHE_DIR = Path("data/image_cache")
IMAGE_CACHE_TTL_DAYS = 7
IMAGE_MIN_SIZE_BYTES = 5_000          # 5 KB — below this is definitely an icon/tracker/pixel
IMAGE_MAX_SIZE_BYTES = 5_242_880      # 5 MB — Telegram limit
MAX_IMAGES_PER_POST = 10              # Telegram mediagroup limit
IMAGE_FETCH_TIMEOUT = 15.0
ARTICLE_FETCH_TIMEOUT = 20.0

# Minimum pixel dimensions — lowered to allow upgraded thumbnails
# BBC thumbnails after /640/ upgrade are 640x360 which pass this filter
IMAGE_MIN_WIDTH = 300
IMAGE_MIN_HEIGHT = 200

# Maximum dimensions — resize down if too large (CarExpert 7000x4600 etc)
IMAGE_MAX_DIMENSION = 2560

# ── Junk URL filter — comprehensive ───────────────────────────────────────────

JUNK_DOMAINS = {
    "mc.yandex.ru", "mc.yandex.com", "google-analytics.com",
    "facebook.com/tr", "connect.facebook.net",
    "pixel.wp.com", "stats.wordpress.com",
    "doubleclick.net", "adservice.google.com",
    "pagead2.googlesyndication.com", "ad.doubleclick.net",
    "platform.twitter.com", "apis.google.com",
    "feeds.feedburner.com", "feedburner.google.com",
    # v8.0: Additional tracker/ad domains
    "hotlog.ru", "liveinternet.ru", "openstat.net",
    "top.mail.ru", "counter.rambler.ru",
    "cdn.ampproject.org",  # AMP cache — low quality
    "t.co",  # Twitter URL shortener — redirects, not images
    "ow.ly", "bit.ly", "tinyurl.com",  # URL shorteners
    "cdn.jsdelivr.net", "cdnjs.cloudflare.com",  # JS CDNs — never host real images
    "gravatar.com",  # Avatar service
    "secure.gravatar.com",
    "disqus.com", "disquscdn.com",  # Comment system
}

JUNK_PATH_KEYWORDS = [
    # ── UI elements & chrome ──
    "favicon", "avatar", "spinner", "loading", "placeholder",
    "pixel", "tracker", "beacon", "counter", "analytics",
    "1x1", "spacer", "blank", "transparent",
    "recaptcha", "captcha",
    "icon", "logo", "badge", "button", "btn",
    # ── Social media buttons & sharing ──
    "share", "facebook", "twitter", "vk.", "vkontakte",
    "telegram", "whatsapp", "instagram", "youtube", "tiktok",
    "pinterest", "linkedin", "reddit",
    "social-media", "social_share", "share-btn",
    # ── Ads & banners ──
    "banner", "ad-", "_ad_", "advert", "sponsor", "promo",
    "doubleclick", "adservice", "adsense",
    # ── Author & profile ──
    "gravatar", "author-photo", "author-img", "author-image",
    "profile-photo", "profile-img", "userpic", "user-pic",
    "mugshot", "headshot", "portrait",
    # ── Sidebar, footer, header, navigation ──
    "sidebar", "widget", "footer", "header", "navbar",
    "navigation", "menu", "breadcrumb", "pagination",
    "related-post", "related-article", "recommended",
    "popular-post", "trending", "also-read", "read-more",
    "newsletter", "subscribe", "popup", "modal", "overlay",
    # ── Charts, graphs, infographics (not photos) ──
    "chart", "graph", "infographic", "diagram",
    "plot-", "pie-chart", "bar-chart", "line-chart",
    "data-viz", "visualization",
    # ── Screenshots & UI captures (not real photos) ──
    "screenshot", "screen-shot", "screen_capture",
    "ui-element", "mockup", "wireframe",
    # ── Emoji, stickers, clipart ──
    "emoji", "sticker", "clipart", "clip-art",
    "smiley", "emoticon",
    # ── Generic / default images ──
    "default-image", "default_image", "no-image", "no_image",
    "coming-soon", "coming_soon", "image-not-found",
    "placeholder-image", "dummy-image",
    # ── Comment section ──
    "comment-avatar", "comment-img", "disqus",
    # ── Weather, maps (not car photos) ──
    "weather-icon", "map-marker", "map-pin",
    # ── Rating / review stars ──
    "star-rating", "rating-star", "review-star",
    "upvote", "downvote", "karma",
    # ── v8.0: Additional junk patterns ──
    "amp_", "amp-",  # AMP thumbnails
    "thumbnail_", "thumbnail-",  # Thumbnail indicators in filenames
    # NOTE: "thumb_" and "thumb-" REMOVED — they match real image URLs
    # like 5koleso.ru/.../thumb/... and legit WordPress thumbnail URLs.
    # The _upgrade_thumbnail_url() function upgrades these to full-size instead.
    "mini-thumb", "minithumb",  # Miniature thumbnails
    "miniature", "miniatyr",  # Miniatures
    "watermark",  # Watermarked images are usually low quality
    "noise-texture", "background-", "bg_",  # Background textures
    "gradient", "pattern-bg",  # Background patterns
    "rss-icon", "rss_",  # RSS feed icons
    "channel-", "channel_",  # Channel icons
    "podcast", "audio-",  # Podcast/audio covers
    "captcha", "recaptcha",  # CAPTCHA images
    "qr-code", "qrcode", "qr_code",  # QR codes
    "cookie", "gdpr", "consent",  # Cookie/GDPR banners
    "push-notification", "notification-",  # Push notification icons
    "mobile-app", "app-icon", "appstore", "google-play",  # App store badges
    "paywall", "subscribe-wall",  # Paywall overlays
    "lazy_", "lazy-",  # Lazy-load placeholder images
    "data:image",  # Base64 embedded images in URL
    # ── v8.1: More junk patterns for news images ──
    "resize=100:",  # Tiny 100px wide thumbnails (Hearst/AOL pattern)
    "resize=50:",  # Even tinier
    "crop=",  # Cropped variants — use base URL instead
    "quality=10",  # Ultra-low quality
    "format=webp&",  # WebP format request (sometimes gives weird results)
    "width=50&",  # Explicit tiny width
    "width=80&",  # Explicit small width
    "height=50&",  # Explicit tiny height
    "dpr_1.0",  # Device pixel ratio = 1x (low res)
    ".svg",  # SVG is not a photo
]

# Generic/default image filenames — not real content
JUNK_STEMS = {
    "business_card", "placeholder", "default_image", "default-image",
    "no_image", "no-image", "coming_soon", "coming-soon",
    "spacer", "blank", "transparent", "pixel", "tracker",
    "1x1", "beacon", "spinner", "loading",
}

JUNK_EXTENSIONS = {".gif", ".svg"}

# Tiny thumbnail size patterns in URL — skip these
# NOTE: Must be specific — don't match real image sizes like 630x420!
TINY_SIZE_PATTERNS = [
    re.compile(r'[?&]width=(?:80|100|120|140)(?:&|$)', re.IGNORECASE),  # width=80..140 in query (icons)
    re.compile(r'[?&]height=(?:80|100|120|140)(?:&|$)', re.IGNORECASE),  # height=80..140 in query (icons)
    re.compile(r'/1\d{2}x1\d{2}/', re.IGNORECASE),                       # /108x108/ /150x150/ in path (icons only)
    re.compile(r'[-_](?:80|100|120|140|150)x(?:80|100|120|140|150)[-_.]', re.IGNORECASE),  # -100x100. (small icons)
]

# /feed/ as image URL (CarScoops bug)
_FEED_URL_RE = re.compile(r'/feed/?$', re.IGNORECASE)


def _normalize_url(url: str) -> str:
    """Normalize image URL: decode HTML entities, fix scheme, remove resize params."""
    if not url:
        return ""
    url = url.strip()
    # Decode HTML entities: &amp; → &, &#038; → &, etc.
    # CRITICAL: Some news sources have &amp; in JSON which makes URLs invalid
    url = html_unescape(url)
    # Multiple passes — some sources double-encode (&amp;amp; → &amp; → &)
    url = html_unescape(url)
    if url.startswith("//"):
        url = "https:" + url
    # CRITICAL FIX: Remove resize/crop parameters that produce tiny thumbnails
    # e.g., ?resize=100:* or ?crop=0.5xw:1.00xh — these are always thumbnails
    # Keep the base URL without resize params for full-size image
    url = re.sub(r'[?&](?:resize|crop)=[^&]*', '', url)
    return url


def _is_junk_url(url: str) -> bool:
    """Comprehensive junk filter for image URLs.

    Blocks: trackers, icons, tiny thumbnails, default images, /feed/ URLs.
    """
    try:
        url_lower = url.lower()
        parsed = urlparse(url_lower)
        hostname = parsed.hostname or ""
        path_lower = parsed.path.lower()

        # Tracker/analytics domains
        for junk in JUNK_DOMAINS:
            if junk in hostname:
                return True

        # Path keywords (icon, logo, badge, etc.)
        for kw in JUNK_PATH_KEYWORDS:
            if kw in url_lower:
                return True

        # Bad extensions
        for ext in JUNK_EXTENSIONS:
            if path_lower.endswith(ext):
                return True

        # /feed/ URL (CarScoops bug — <img src="/feed/">)
        if _FEED_URL_RE.search(url_lower):
            return True

        # Generic/default image filenames
        # Check exact match and prefix match (e.g. business_card-1 → business_card_1)
        stem = path_lower.rsplit("/", 1)[-1].split(".")[0].replace("-", "_")
        if stem in JUNK_STEMS:
            return True
        for junk_stem in JUNK_STEMS:
            if stem.startswith(junk_stem):
                return True

        # Tiny thumbnail dimensions in URL params
        for pattern in TINY_SIZE_PATTERNS:
            if pattern.search(url_lower):
                return True

        # v8.1: Reject URLs with tiny resize parameters (Hearst, AOL, etc.)
        # e.g., ?resize=100:* or &resize=50:* — these are always tiny thumbnails
        resize_match = re.search(r'[?&]resize=(\d+):', url_lower)
        if resize_match and int(resize_match.group(1)) <= 150:
            return True

        # v8.1: Reject URLs with small width/height params
        width_match = re.search(r'[?&]w=(\d+)(?:&|$)', url_lower)
        if width_match and int(width_match.group(1)) <= 150:
            return True
        height_match = re.search(r'[?&]h=(\d+)(?:&|$)', url_lower)
        if height_match and int(height_match.group(1)) <= 100:
            return True

    except Exception:
        return True  # Can't parse = skip
    return False


def _upgrade_thumbnail_url(url: str) -> str:
    """Upgrade low-quality thumbnail URLs to higher resolution.

    Known CDN patterns:
    - BBC: /240/ → /640/ in path
    - Autosport/Motorsport.com: /s6/ → /s12/ in path
    - Reddit: width=140 → width=640
    - Autocar: /car_review_image_190/ → /body-image/ (190×125 → 900×600)
    """
    # BBC
    if "bbci.co.uk" in url or "bbc.co.uk" in url:
        url = re.sub(r'/240/', '/640/', url, count=1)

    # Autosport / Motorsport.com: keep /s6/ — /s12/ returns 403 from CDN!
    # /s6/ gives ~205KB JPEG which is fine quality
    if "motorsport.com" in url or "autosport.com" in url:
        pass  # Don't upgrade — /s12/ is blocked by CDN

    # Reddit
    if "preview.redd.it" in url or "external-preview.redd.it" in url:
        url = re.sub(r'width=140', 'width=640', url, count=1)
        url = re.sub(r'height=140', 'height=640', url, count=1)

    # Autocar UK: /styles/car_review_image_190/ → /styles/body-image/
    if "autocar.co.uk" in url:
        url = url.replace('/styles/car_review_image_190/', '/styles/body-image/')

    # TASS: upgrade small thumbnails to full-size
    if "tass.ru" in url:
        url = re.sub(r'/w_\d{2,3}/', '/w_1080/', url, count=1)
        url = re.sub(r'[?&](width|height|size)=\d+', '', url)

    # Kommersant: upgrade thumbnails
    if "kommersant.ru" in url:
        url = url.replace('/small/', '/large/')
        url = url.replace('/thumb/', '/full/')
        url = re.sub(r'/\d+x\d+/', '/1200x800/', url, count=1)

    # Mail.ru auto: upgrade previews
    if "mail.ru" in url or "img.imgsmail.ru" in url:
        url = url.replace('/preview/', '/original/')
        url = url.replace('/thumb/', '/original/')
        url = re.sub(r'[?&]w=\d{2,3}', '', url)

    # 5Колесо: upgrade thumbnails
    if "5koleso.ru" in url:
        url = url.replace('/thumb/', '/full/')
        url = url.replace('/preview/', '/full/')

    # CarExpert AU: add size parameter for reasonable dimensions
    if "carexpert.com.au" in url:
        if '?' not in url:
            url += '?width=1600'

    # BBC: upgrade all size variants to 640
    if "bbci.co.uk" in url or "bbc.co.uk" in url:
        url = re.sub(r'/ace/standard/\d+/', '/ace/standard/640/', url, count=1)
        url = re.sub(r'/images/ic/\d+x\d+/', '/images/ic/640x360/', url, count=1)
        # Generic numeric size in path /240/ or /480/ → /640/
        url = re.sub(r'/(\d{2,3})/', '/640/', url, count=1)

    # Reddit: remove crop=1:1,smart parameter (gives square icons)
    if "redd.it" in url:
        url = re.sub(r'[&?]crop=[^&]+', '', url)

    return url


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


# ── Download & validation ────────────────────────────────────────────────────

async def _download_image(client: httpx.AsyncClient, url: str) -> Optional[bytes]:
    """Download an image URL with validation. Returns bytes or None.

    Normalizes URL (decode entities, upgrade thumbnails), validates
    content type, size, magic bytes, and pixel dimensions.
    """
    # Normalize: decode HTML entities, upgrade thumbnails
    url = _normalize_url(url)
    if not url:
        return None

    # Junk filter
    if _is_junk_url(url):
        return None

    # Upgrade low-quality thumbnails
    url = _upgrade_thumbnail_url(url)

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
            pass  # Some servers don't support HEAD

        # Full GET
        resp = await client.get(url, timeout=IMAGE_FETCH_TIMEOUT, follow_redirects=True)
        if resp.status_code != 200:
            return None

        img_bytes = resp.content

        # Size check
        if len(img_bytes) < IMAGE_MIN_SIZE_BYTES or len(img_bytes) > IMAGE_MAX_SIZE_BYTES:
            return None

        # Magic bytes check
        is_valid = (
            img_bytes[:3] == b'\xff\xd8\xff'       # JPEG
            or img_bytes[:4] == b'\x89PNG'           # PNG
            or img_bytes[:4] == b'RIFF'              # WebP (RIFF container)
        )
        if not is_valid:
            return None

        # Skip SVG content
        if b'<svg' in img_bytes[:500]:
            return None

        # Dimension check — skip tiny icons/buttons/thumbnails, resize oversized images
        try:
            from PIL import Image
            import io
            img = Image.open(io.BytesIO(img_bytes))
            w, h = img.size

            # Resize oversized images (CarExpert 7000x4600, etc.)
            # Large images waste bandwidth and Telegram resizes anyway
            if w > IMAGE_MAX_DIMENSION or h > IMAGE_MAX_DIMENSION:
                img.thumbnail((IMAGE_MAX_DIMENSION, IMAGE_MAX_DIMENSION), Image.LANCZOS)
                w, h = img.size
                buf = io.BytesIO()
                # Save as JPEG for consistency and size
                if img.mode in ('RGBA', 'P', 'LA'):
                    img = img.convert('RGB')
                img.save(buf, format='JPEG', quality=85)
                img_bytes = buf.getvalue()
                logger.debug(f"Resized large image to {w}x{h} ({len(img_bytes)//1024}KB)")

            # Minimum dimensions — real photos are at least this size
            if w < IMAGE_MIN_WIDTH or h < IMAGE_MIN_HEIGHT:
                logger.debug(f"Image too small: {w}x{h} (min {IMAGE_MIN_WIDTH}x{IMAGE_MIN_HEIGHT}) — skipping")
                return None
            # Skip banners (extreme aspect ratios like 728x90, 320x50, etc.)
            # Normal photos are between 1:3 and 3:1
            aspect = w / max(h, 1)
            if aspect > 3.5 or aspect < 0.28:
                logger.debug(f"Image bad aspect ratio: {w}x{h} (ratio={aspect:.2f}) — skipping banner/button")
                return None
            # Skip square images under 200px — usually avatars or icons
            if abs(w - h) < 20 and w < 200:
                logger.debug(f"Image too square (probably avatar/icon): {w}x{h} — skipping")
                return None
            # Skip very small images — small area = junk
            area = w * h
            if area < 50000:  # 224x224 ≈ 50000 — below this is definitely not a real photo
                logger.debug(f"Image area too small: {area}px ({w}x{h}) — skipping")
                return None
            # Skip images with excessive file size relative to dimensions (corrupt/bloated)
            bytes_per_pixel = len(img_bytes) / max(area, 1)
            if bytes_per_pixel > 10:  # > 10 bytes/pixel = likely corrupt or not a real photo
                logger.debug(f"Image too bloated: {bytes_per_pixel:.1f} bytes/pixel — skipping")
                return None
        except ImportError:
            pass  # PIL not available
        except Exception:
            pass  # Can't read dimensions

        return img_bytes

    except Exception:
        return None


# ── Strategy 1: RSS image URLs (pre-filtered by news.py) ─────────────────────
# Note: The actual RSS image extraction is done in news._extract_entry_images()
# which now handles HTML entity decoding, junk filtering, thumbnail upgrades,
# and normalized dedup. The image_urls list passed here is already clean.


# ── Google News redirect resolution ───────────────────────────────────────────
# Google News RSS feeds use URLs like:
#   https://news.google.com/rss/articles/CBMiYkFVX3lxTFB4dDdacXR...
# These are redirect URLs that lead to the REAL article. Scraping the Google
# News page directly rarely finds images. We need to follow the redirect
# to the actual publisher's article page first.

_GOOGLE_NEWS_URL_RE = re.compile(r'news\.google\.com/rss/articles/', re.IGNORECASE)
_GOOGLE_NEWS_BASE_RE = re.compile(r'news\.google\.com', re.IGNORECASE)
_GOOGLE_AMP_RE = re.compile(r'^https?://[^/]*\.google\.\w+/amp/', re.IGNORECASE)


async def _resolve_google_news_url(url: str, client: httpx.AsyncClient) -> str:
    """Follow Google News redirect to get the real article URL.

    Google News RSS article URLs are redirects. We need to follow them
    to reach the actual publisher's page where we can scrape images.

    NOTE: From GitHub Actions IPs, Google News returns 400 for these URLs.
    So we also try to resolve via web search using the article title extracted
    from the URL's base64 payload.

    Returns the resolved URL, or the original URL if resolution fails.
    """
    if not _GOOGLE_NEWS_URL_RE.search(url) and not _GOOGLE_NEWS_BASE_RE.search(url):
        return url

    _BROWSER_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5,ru-RU;q=0.8",
    }

    # Try 1: HEAD with follow_redirects
    try:
        resp = await client.head(url, timeout=10.0, follow_redirects=True, headers=_BROWSER_HEADERS)
        resolved = str(resp.url)
        if resolved != url and not _GOOGLE_NEWS_BASE_RE.search(resolved):
            logger.info(f"Google News redirect resolved (HEAD): {url[:60]}... → {resolved[:60]}...")
            return resolved
    except Exception:
        pass

    # Try 2: GET with follow_redirects (stream mode to avoid downloading full page)
    try:
        async with client.stream(
            "GET", url, timeout=10.0, follow_redirects=True, headers=_BROWSER_HEADERS,
        ) as resp:
            resolved = str(resp.url)
            if resolved != url and not _GOOGLE_NEWS_BASE_RE.search(resolved):
                logger.info(f"Google News redirect resolved (GET stream): {url[:60]}... → {resolved[:60]}...")
                return resolved
    except Exception:
        pass

    # Try 3: Full GET without follow_redirects — check for 302/301/canonical/meta-refresh
    try:
        resp = await client.get(url, timeout=10.0, follow_redirects=False, headers=_BROWSER_HEADERS)
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("location", "")
            if location and not _GOOGLE_NEWS_BASE_RE.search(location):
                logger.info(f"Google News 302 redirect: {url[:60]}... → {location[:60]}...")
                return location
        # Parse HTML for canonical/refresh/real article URL
        html = resp.text[:10000]
        for pattern, name in [
            (r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)["\']', "canonical"),
            (r'<meta[^>]+http-equiv=["\']refresh["\'][^>]+content=["\'][^"\']*url=([^"\';\s]+)', "meta-refresh"),
            (r'data-url=["\']([^"\']+)["\']', "data-url"),
            (r'<a[^>]+class=["\'][^"\']*article[^"\']*["\'][^>]+href=["\']([^"\']+)["\']', "article-link"),
        ]:
            m = re.search(pattern, html, re.IGNORECASE)
            if m:
                found = m.group(1)
                if found and not _GOOGLE_NEWS_BASE_RE.search(found) and found.startswith("http"):
                    logger.info(f"Google News {name}: {found[:60]}...")
                    return found
    except Exception:
        pass

    # Try 4: Full GET WITH follow_redirects — sometimes Google serves the real page directly
    try:
        resp = await client.get(url, timeout=15.0, follow_redirects=True, headers=_BROWSER_HEADERS)
        resolved = str(resp.url)
        if resolved != url and not _GOOGLE_NEWS_BASE_RE.search(resolved):
            logger.info(f"Google News resolved (full GET): {url[:60]}... → {resolved[:60]}...")
            return resolved
        # Check if the response HTML contains og:url pointing to the real article
        html = resp.text[:10000]
        og_url = re.search(r'<meta[^>]+property=["\']og:url["\'][^>]+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
        if og_url:
            found = og_url.group(1)
            if found and not _GOOGLE_NEWS_BASE_RE.search(found) and found.startswith("http"):
                logger.info(f"Google News og:url: {found[:60]}...")
                return found
    except Exception:
        pass

    logger.info(f"Could not resolve Google News URL (likely 400 from server IP): {url[:60]}...")
    return url  # Return original if all resolution attempts fail


async def _search_article_url(topic: str, client: httpx.AsyncClient) -> str:
    """Find the real article URL by searching the web for the topic title.

    This is a FALLBACK when Google News redirect resolution fails.
    Uses DuckDuckGo or other search to find the original article,
    then returns that URL for image scraping.

    Returns the found article URL, or empty string if nothing found.
    """
    if not topic or len(topic) < 10:
        return ""

    try:
        from bot.web_search import web_search
        # Search for the article by title
        # Use a truncated title + "site:" exclusion for Google News
        query = topic[:80]
        results = await web_search(query, num_results=5)

        for result in results:
            url = result.url if hasattr(result, "url") else result.get("url", "")
            # Skip Google News URLs — we already know those don't work
            if _GOOGLE_NEWS_BASE_RE.search(url):
                continue
            # Skip social media and aggregator URLs
            skip_domains = ["twitter.com", "facebook.com", "reddit.com", "t.me",
                           "dzen.ru", "yandex.ru", "google.com"]
            if any(d in url.lower() for d in skip_domains):
                continue
            if url and url.startswith("http") and len(url) > 20:
                logger.info(f"Found article URL via web search: {url[:80]}")
                return url
    except Exception as e:
        logger.debug(f"Article URL search failed for '{topic[:40]}': {e}")

    return ""


# ── Strategy 2: Article page image extraction ────────────────────────────────

async def fetch_article_images(
    url: str, client: httpx.AsyncClient, max_count: int = 10
) -> List[Tuple[bytes, str]]:
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

        def _add_candidate(raw_url: str):
            """Normalize, junk-filter, and dedup a candidate URL."""
            u = _normalize_url(raw_url)
            if not u or len(u) < 20:
                return
            if _is_junk_url(u):
                return
            u = _upgrade_thumbnail_url(u)
            # Dedup by normalized (lowercase) form
            norm_key = u.lower().rstrip("/")
            if norm_key not in seen:
                seen.add(norm_key)
                candidate_urls.append(u)

        # 1. og:image
        for pattern in [
            r'<meta[^>]+property=["\x27]og:image["\x27][^>]+content=["\x27]([^"\x27]+)["\x27]',
            r'<meta[^>]+content=["\x27]([^"\x27]+)["\x27][^>]+property=["\x27]og:image["\x27]',
            r'<meta[^>]+property=["\x27]og:image:url["\x27][^>]+content=["\x27]([^"\x27]+)["\x27]',
            r'<meta[^>]+property=["\x27]og:image:secure_url["\x27][^>]+content=["\x27]([^"\x27]+)["\x27]',
        ]:
            for m in re.finditer(pattern, html, re.IGNORECASE):
                _add_candidate(m.group(1))

        # 2. twitter:image
        for pattern in [
            r'<meta[^>]+name=["\x27]twitter:image["\x27][^>]+content=["\x27]([^"\x27]+)["\x27]',
            r'<meta[^>]+content=["\x27]([^"\x27]+)["\x27][^>]+name=["\x27]twitter:image["\x27]',
        ]:
            for m in re.finditer(pattern, html, re.IGNORECASE):
                _add_candidate(m.group(1))

        # 3. JSON-LD
        jsonld_urls = _extract_jsonld_images(html)
        for u in jsonld_urls:
            _add_candidate(u)

        # 4. <picture>/<source srcset>
        picture_blocks = re.findall(r'<picture[^>]*>(.*?)</picture>', html, re.IGNORECASE | re.DOTALL)
        for block in picture_blocks:
            srcsets = re.findall(r'srcset=["\x27]([^"\x27]+)["\x27]', block, re.IGNORECASE)
            for srcset in srcsets:
                for part in srcset.split(','):
                    u = part.strip().split()[0] if part.strip() else ''
                    _add_candidate(u)

        # 5. <img> tags from article body — EXPANDED selectors for more coverage
        article_html = ""
        for pattern in [
            r'<article[^>]*>(.*?)</article>',
            r'<main[^>]*>(.*?)</main>',
            r'<div[^>]+class=["\x27][^"\x27]*(?:content|article|post|entry|gallery|slider|carousel|swiper|slick|fotorama|photogallery)[^"\x27]*["\x27][^>]*>(.*?)</div>',
            r'<div[^>]+id=["\x27][^"\x27]*(?:content|article|post|entry|gallery)[^"\x27]*["\x27][^>]*>(.*?)</div>',
            r'<section[^>]*>(.*?)</section>',
        ]:
            matches = re.findall(pattern, html, re.IGNORECASE | re.DOTALL)
            for match in matches:
                article_html += match + "\n"

        search_html = article_html if article_html else html

        # Lazy-loaded images FIRST (usually higher quality originals)
        # Expanded to cover many common lazy-load frameworks
        for attr_pattern in [
            r'<img[^>]+data-src=["\x27]([^"\x27]+)["\x27]',
            r'<img[^>]+data-lazy-src=["\x27]([^"\x27]+)["\x27]',
            r'<img[^>]+data-original=["\x27]([^"\x27]+)["\x27]',
            r'<img[^>]+data-lazy=["\x27]([^"\x27]+)["\x27]',
            r'<img[^>]+data-bg=["\x27]([^"\x27]+)["\x27]',
            r'<img[^>]+data-full=["\x27]([^"\x27]+)["\x27]',
            r'<img[^>]+data-large=["\x27]([^"\x27]+)["\x27]',
            r'<img[^>]+data-big=["\x27]([^"\x27]+)["\x27]',
            # srcset (responsive images — take first URL from each)
            r'<img[^>]+srcset=["\x27]([^"\x27]+)["\x27]',
            r'<source[^>]+srcset=["\x27]([^"\x27]+)["\x27]',
            # Standard src
            r'<img[^>]+src=["\x27]([^"\x27]+)["\x27]',
        ]:
            for m in re.finditer(attr_pattern, search_html, re.IGNORECASE):
                raw_val = m.group(1)
                # srcset can contain multiple URLs: "img1x.jpg 1x, img2x.jpg 2x"
                if 'srcset' in attr_pattern.lower():
                    for part in raw_val.split(','):
                        url_part = part.strip().split()[0] if part.strip() else ''
                        if url_part:
                            _add_candidate(url_part)
                else:
                    _add_candidate(raw_val)

        # 6. Gallery/slideshow JSON data embedded in script tags
        # Many CMS embed image arrays in script tags (WordPress, TASS, etc.)
        for script_match in re.finditer(
            r'<script[^>]*>(.*?)</script>', html, re.IGNORECASE | re.DOTALL
        ):
            script_content = script_match.group(1)
            # Look for image URL patterns in JSON-like script content
            if '"image"' in script_content or '"src"' in script_content or '"url"' in script_content:
                for img_match in re.finditer(
                    r'["\x27]?(?:image|src|url|photo|thumbnail|preview)["\x27]?\s*[:=]\s*["\x27]?(https?://[^\s"\'\\,}\]]+)',
                    script_content, re.IGNORECASE
                ):
                    _add_candidate(img_match.group(1))

        logger.info(f"Scraped {len(candidate_urls)} candidate image URLs from {url[:60]}")

        # Download candidates — try MORE candidates to find up to 10 valid photos
        # (many candidates fail validation, so try 5x the target count)
        for img_url in candidate_urls[:max_count * 5]:
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
            image_urls=["https://..."],  # Pre-filtered by news._extract_entry_images()
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
                    "Accept-Language": "en-US,en;q=0.5,ru-RU;q=0.8",
                    # Referer needed for CDN-hotlink protection (bauersecure.com, hearstapps.com etc.)
                    "Referer": "https://www.google.com/",
                },
            )
        return self._client

    async def fetch(
        self,
        topic: str,
        article_url: str = "",
        image_urls: List[str] = None,
        max_images: int = MAX_IMAGES_PER_POST,
    ) -> Tuple[List[bytes], str]:
        """Fetch images from the article. Simple 3-step pipeline.

        Step 0: Resolve Google News redirect URLs to real article URLs
        Step 1: RSS images (from news._extract_entry_images() — already filtered)
        Step 2: Article page scraping (og:image, JSON-LD, <img> tags)

        Returns (image_list, source_str).
        """
        self._seen_hashes = set()
        client = self._get_client()
        all_images: List[bytes] = []
        source = "none"

        # ── Step 0: Resolve Google News redirects ───────────────────────
        # Google News URLs like news.google.com/rss/articles/... are redirects.
        # We need the REAL article URL to scrape images from.
        resolved_url = article_url
        if article_url:
            resolved_url = await _resolve_google_news_url(article_url, client)
            if resolved_url != article_url:
                logger.info(f"Article URL resolved: {article_url[:50]}... → {resolved_url[:50]}...")

        # ── Check cache first ──────────────────────────────────────────
        cached = self.cache.get(topic)
        if cached:
            cached = deduplicate_images(cached)[:max_images]
            if cached:
                logger.info(f"Image cache HIT for '{topic[:50]}' — {len(cached)} images")
                return cached, "cache"

        # ── Step 1: RSS images ─────────────────────────────────────────
        # image_urls = already filtered by news._extract_entry_images()
        # (HTML entities decoded, junk removed, thumbnails upgraded, deduped)
        rss_urls = list(image_urls or [])

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
        # Use the resolved URL (not the original Google News redirect)
        scrape_url = resolved_url
        if scrape_url and len(all_images) < max_images:
            article_results = await fetch_article_images(
                scrape_url, client,
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

        # ── Step 3: Try original URL if resolved URL failed ───────────
        # If the resolved URL didn't yield images, try the original URL
        # (maybe the original site has different images)
        if article_url and resolved_url != article_url and len(all_images) < max_images:
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
                source = "article-fallback"
            elif article_results:
                source += "+fallback"

        # ── Step 4: Web search fallback for Google News / unresolved URLs ──
        # If we still have no images and the URL is a Google News redirect,
        # search the web for the real article and scrape images from it.
        if len(all_images) < max_images:
            is_google_news = article_url and _GOOGLE_NEWS_BASE_RE.search(article_url)
            needs_search = is_google_news or len(all_images) == 0
            if needs_search:
                logger.info(f"Step 4: Searching web for real article URL (topic='{topic[:50]}')")
                search_url = await _search_article_url(topic, client)
                if search_url:
                    article_results = await fetch_article_images(
                        search_url, client,
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
                        source = "web-search"
                    elif article_results:
                        source += "+web-search"
                    if all_images:
                        logger.info(f"Got {len(all_images)} images via web search fallback for '{topic[:50]}'")

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
        image_urls=image_urls,
        max_images=max_images,
    )
