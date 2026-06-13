"""Article Fetcher — fetch full article pages, extract text & images.

PURPOSE: RSS feeds often provide only a short summary with no images.
Google News RSS gives only a redirect URL with NO content at all.
This module resolves those issues by:
  1. Resolving Google News redirect URLs to real article URLs
  2. Fetching the full article page
  3. Extracting full article text (for fact-gathering)
  4. Extracting all quality images from the page

This ensures posts have:
  - Full facts (not just the RSS snippet)
  - Quality photos (up to 10 per post)
  - Proper attribution to the source article
"""

from __future__ import annotations

import logging
import re
import time
from html import unescape as html_unescape
from html.parser import HTMLParser
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse, urljoin

import httpx

logger = logging.getLogger("asya.article_fetcher")

# ── Configuration ─────────────────────────────────────────────────────────────

FETCH_TIMEOUT = 20.0
MAX_ARTICLE_LENGTH = 8000  # Max chars of article text to extract
MAX_IMAGES_FROM_ARTICLE = 10

# ── Google News redirect resolution ───────────────────────────────────────────

_GOOGLE_NEWS_URL_RE = re.compile(r'news\.google\.com/rss/articles/', re.IGNORECASE)
_GOOGLE_NEWS_BASE_RE = re.compile(r'news\.google\.com', re.IGNORECASE)

_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5,ru-RU;q=0.8",
}


async def resolve_google_news_url(url: str, client: Optional[httpx.AsyncClient] = None) -> str:
    """Follow Google News redirect to get the real article URL.
    
    Tries multiple strategies:
    1. HEAD request with follow_redirects
    2. GET stream with follow_redirects  
    3. Parse HTML for canonical/meta-refresh
    4. Full GET with follow_redirects
    
    Returns the resolved URL, or the original URL if resolution fails.
    """
    if not url:
        return url
    
    # Not a Google News URL — return as-is
    if not _GOOGLE_NEWS_BASE_RE.search(url):
        return url

    should_close = False
    if client is None:
        client = httpx.AsyncClient(timeout=FETCH_TIMEOUT, follow_redirects=True)
        should_close = True

    try:
        # Try 1: HEAD with follow_redirects
        try:
            resp = await client.head(url, timeout=10.0, follow_redirects=True, headers=_BROWSER_HEADERS)
            resolved = str(resp.url)
            if resolved != url and not _GOOGLE_NEWS_BASE_RE.search(resolved):
                logger.info(f"Google News resolved (HEAD): {url[:50]}... → {resolved[:50]}...")
                return resolved
        except Exception:
            pass

        # Try 2: GET with follow_redirects (stream mode)
        try:
            async with client.stream(
                "GET", url, timeout=10.0, follow_redirects=True, headers=_BROWSER_HEADERS,
            ) as resp:
                resolved = str(resp.url)
                if resolved != url and not _GOOGLE_NEWS_BASE_RE.search(resolved):
                    logger.info(f"Google News resolved (stream): {url[:50]}... → {resolved[:50]}...")
                    return resolved
        except Exception:
            pass

        # Try 3: Full GET — parse HTML for canonical/meta-refresh/og:url
        try:
            resp = await client.get(url, timeout=15.0, follow_redirects=True, headers=_BROWSER_HEADERS)
            resolved = str(resp.url)
            if resolved != url and not _GOOGLE_NEWS_BASE_RE.search(resolved):
                logger.info(f"Google News resolved (GET): {url[:50]}... → {resolved[:50]}...")
                return resolved
            
            html = resp.text[:15000]
            # Check for og:url, canonical, meta-refresh
            for pattern, name in [
                (r'<meta[^>]+property=["\']og:url["\'][^>]+content=["\']([^"\']+)["\']', "og:url"),
                (r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)["\']', "canonical"),
                (r'<meta[^>]+http-equiv=["\']refresh["\'][^>]+content=["\'][^"\']*url=([^"\';\s]+)', "meta-refresh"),
                (r'data-url=["\']([^"\']+)["\']', "data-url"),
            ]:
                m = re.search(pattern, html, re.IGNORECASE)
                if m:
                    found = m.group(1)
                    if found and not _GOOGLE_NEWS_BASE_RE.search(found) and found.startswith("http"):
                        logger.info(f"Google News resolved ({name}): {found[:60]}...")
                        return found
        except Exception:
            pass

        logger.info(f"Could not resolve Google News URL: {url[:60]}...")
        return url
    finally:
        if should_close:
            await client.aclose()


# ── Article text extraction ───────────────────────────────────────────────────

class _ArticleTextExtractor(HTMLParser):
    """Extract readable article text from HTML.
    
    Heuristic approach:
    - Look for <article>, <main>, or high-content-density <div> blocks
    - Extract text from <p>, <h1>-<h6>, <li>, <blockquote> tags
    - Skip scripts, styles, nav, footer, sidebar, comments
    - Return the longest coherent text block found
    """
    
    SKIP_TAGS = {'script', 'style', 'nav', 'footer', 'header', 'aside',
                 'noscript', 'iframe', 'svg', 'form', 'button', 'input',
                 'select', 'textarea', 'label', 'figure', 'figcaption',
                 'comment', 'comments', 'sidebar', 'advertisement'}
    TEXT_TAGS = {'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li',
                'blockquote', 'pre', 'span', 'div', 'td', 'th'}
    
    def __init__(self):
        super().__init__()
        self.text_blocks: List[str] = []
        self._current_text = ""
        self._skip_depth = 0
        self._tag_stack: List[str] = []
        self._in_article = False
        self._article_depth = 0
        self._main_depth = 0
    
    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]):
        tag_lower = tag.lower()
        self._tag_stack.append(tag_lower)
        
        # Track if we're inside <article> or <main>
        if tag_lower == 'article':
            self._in_article = True
            self._article_depth = len(self._tag_stack)
        elif tag_lower == 'main':
            self._main_depth = len(self._tag_stack)
        
        # Check for skip-indicating classes/roles
        attr_dict = dict(attrs)
        class_attr = (attr_dict.get('class') or '').lower()
        role_attr = (attr_dict.get('role') or '').lower()
        id_attr = (attr_dict.get('id') or '').lower()
        
        skip_indicators = ['sidebar', 'comment', 'footer', 'header', 'nav',
                          'advertisement', 'promo', 'related', 'social',
                          'share', 'cookie', 'banner', 'popup', 'modal',
                          'newsletter', 'subscription', 'paywall']
        
        if any(ind in class_attr or ind in id_attr or ind in role_attr 
               for ind in skip_indicators):
            self._skip_depth += 1
        
        if tag_lower in self.SKIP_TAGS:
            self._skip_depth += 1
        
        # End current text block on block-level elements
        if tag_lower in ('p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'div',
                         'br', 'li', 'blockquote', 'tr'):
            if self._current_text.strip():
                self.text_blocks.append(self._current_text.strip())
            self._current_text = ""
    
    def handle_endtag(self, tag: str):
        tag_lower = tag.lower()
        
        if self._current_text.strip():
            self.text_blocks.append(self._current_text.strip())
        self._current_text = ""
        
        if tag_lower in self.SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        
        if tag_lower == 'article':
            self._in_article = False
        elif tag_lower == 'main':
            self._main_depth = 0
        
        if self._tag_stack:
            self._tag_stack.pop()
    
    def handle_data(self, data: str):
        if self._skip_depth > 0:
            return
        text = data.strip()
        if text:
            if self._current_text:
                self._current_text += " " + text
            else:
                self._current_text = text
    
    def get_article_text(self) -> str:
        """Return the extracted article text, cleaned and limited."""
        # Flush remaining text
        if self._current_text.strip():
            self.text_blocks.append(self._current_text.strip())
        
        # Filter out very short blocks (likely UI elements)
        meaningful_blocks = [b for b in self.text_blocks if len(b) > 20]
        
        if not meaningful_blocks:
            return ""
        
        # Join blocks into article text
        text = "\n\n".join(meaningful_blocks)
        
        # Clean up
        text = re.sub(r'\n{3,}', '\n\n', text)
        text = re.sub(r' {2,}', ' ', text)
        
        # Limit length
        if len(text) > MAX_ARTICLE_LENGTH:
            text = text[:MAX_ARTICLE_LENGTH] + "..."
        
        return text.strip()


def _extract_article_text(html: str) -> str:
    """Extract readable article text from HTML using heuristic parser.
    
    Strategy:
    1. Remove scripts, styles, nav, footer, sidebar, comments
    2. Extract text from <p> tags (primary content)
    3. Also extract <h2>/<h3> headings for structure
    4. Join and clean up
    """
    try:
        # Remove unwanted sections first
        # Remove script/style/nav/footer/aside/comment blocks
        html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r'<nav[^>]*>.*?</nav>', '', html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r'<footer[^>]*>.*?</footer>', '', html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r'<aside[^>]*>.*?</aside>', '', html, flags=re.DOTALL | re.IGNORECASE)
        # Remove noscript, iframe, svg
        html = re.sub(r'<noscript[^>]*>.*?</noscript>', '', html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r'<iframe[^>]*>.*?</iframe>', '', html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r'<svg[^>]*>.*?</svg>', '', html, flags=re.DOTALL | re.IGNORECASE)
        
        # Remove divs with sidebar/comment/ad/nav/footer/header classes
        for cls_pattern in ['sidebar', 'comment', 'footer', 'header', 'nav-',
                           'advertisement', 'promo', 'related', 'social',
                           'share', 'cookie', 'banner', 'popup', 'modal',
                           'newsletter', 'subscription', 'paywall', 'toc']:
            html = re.sub(
                rf'<div[^>]+class=["\'][^"\']*{cls_pattern}[^"\']*["\'][^>]*>.*?</div>',
                '', html, flags=re.DOTALL | re.IGNORECASE
            )
        
        # Extract text from <p> tags (main article content)
        paragraphs = []
        for m in re.finditer(r'<p[^>]*>(.*?)</p>', html, re.DOTALL | re.IGNORECASE):
            text = m.group(1)
            # Remove inner HTML tags
            text = re.sub(r'<[^>]+>', '', text)
            # Decode entities
            text = html_unescape(text).strip()
            # Skip very short paragraphs (likely UI elements)
            if len(text) < 30:
                continue
            # Skip menu/navigation items (ALL CAPS, short, with arrows)
            if text == text.upper() and len(text) < 60:
                continue
            # Skip paragraphs that are just punctuation or whitespace
            if not re.search(r'[a-zA-Zа-яА-ЯёЁ]', text):
                continue
            # Skip paragraphs with too many consecutive newlines/spaces (menu content)
            if text.count('\n') > 5:
                continue
            paragraphs.append(text)
        
        # Also extract <h2> and <h3> headings (helps with structure)
        for m in re.finditer(r'<h[23][^>]*>(.*?)</h[23]>', html, re.DOTALL | re.IGNORECASE):
            text = m.group(1)
            text = re.sub(r'<[^>]+>', '', text)
            text = html_unescape(text).strip()
            if len(text) > 10:
                paragraphs.append(text)
        
        if not paragraphs:
            # Fallback: try the HTMLParser approach
            extractor = _ArticleTextExtractor()
            extractor.feed(html)
            return extractor.get_article_text()
        
        # Join paragraphs
        text = "\n\n".join(paragraphs)
        
        # Clean up
        text = re.sub(r'\n{3,}', '\n\n', text)
        text = re.sub(r' {2,}', ' ', text)
        
        # Limit length
        if len(text) > MAX_ARTICLE_LENGTH:
            text = text[:MAX_ARTICLE_LENGTH] + "..."
        
        return text.strip()
    except Exception as e:
        logger.debug(f"Article text extraction failed: {e}")
        return ""


# ── Image URL extraction from article page ────────────────────────────────────

_JUNK_DOMAINS = {
    "mc.yandex.ru", "mc.yandex.com", "google-analytics.com",
    "facebook.com/tr", "connect.facebook.net",
    "pixel.wp.com", "stats.wordpress.com",
    "doubleclick.net", "adservice.google.com",
    "pagead2.googlesyndication.com", "ad.doubleclick.net",
    "platform.twitter.com", "apis.google.com",
    "cdn.ampproject.org", "t.co",
    "gravatar.com", "secure.gravatar.com",
    "disqus.com", "disquscdn.com",
    "hotlog.ru", "liveinternet.ru", "openstat.net",
    "top.mail.ru", "counter.rambler.ru",
}

_JUNK_PATH_KEYWORDS = [
    "favicon", "avatar", "spinner", "loading", "placeholder",
    "pixel", "tracker", "beacon", "counter", "analytics",
    "1x1", "spacer", "blank", "transparent",
    "recaptcha", "captcha", "icon", "logo", "badge",
    "button", "btn", "share", "social",
    "facebook", "twitter", "vk.", "vkontakte", "telegram",
    "whatsapp", "instagram", "youtube", "tiktok",
    "pinterest", "linkedin", "reddit",
    "advert", "banner", "sponsor", "promo",
    "newsletter", "subscribe", "paywall",
    "emoji", "smileys", "rating", "star",
]


def _is_junk_image_url(url: str) -> bool:
    """Check if an image URL is junk (tracking pixel, icon, ad, etc.)."""
    lower = url.lower()
    # Check domain
    parsed = urlparse(lower)
    domain = parsed.netloc.lower()
    for junk_domain in _JUNK_DOMAINS:
        if junk_domain in domain:
            return True
    # Check path
    path = parsed.path.lower()
    for kw in _JUNK_PATH_KEYWORDS:
        if kw in path:
            return True
    # Skip SVG, GIF animations
    if path.endswith('.svg') or path.endswith('.gif'):
        return True
    # Skip data URIs
    if lower.startswith('data:'):
        return True
    # Skip schema/logo URLs (JSON-LD artifacts like /#/schema/logo/image/)
    if '/schema/' in path or '/schema/logo' in path:
        return True
    # Skip hash URLs (/#/)
    if '/#/' in url:
        return True
    return False


def _normalize_image_url(url: str, base_url: str = "") -> str:
    """Normalize and resolve a potentially relative image URL."""
    url = html_unescape(url).strip()
    url = url.replace('&amp;', '&')
    
    if url.startswith('//'):
        url = 'https:' + url
    elif url.startswith('/'):
        if base_url:
            url = urljoin(base_url, url)
        else:
            return ""
    elif not url.startswith('http'):
        if base_url:
            url = urljoin(base_url, url)
        else:
            return ""
    
    # Skip very short URLs (likely tracking)
    if len(url) < 30:
        return ""
    
    return url


def extract_image_urls_from_html(html: str, base_url: str = "") -> List[str]:
    """Extract all quality image URLs from an HTML page.
    
    Priority order:
    1. og:image / twitter:image (editor-chosen hero images)
    2. JSON-LD structured data images
    3. <picture>/<source srcset> (responsive images)
    4. <img> tags (data-src first for lazy-loaded, then src)
    
    Returns deduplicated, junk-filtered list of image URLs.
    """
    images = []
    seen = set()

    def _add(raw_url: str):
        url = _normalize_image_url(raw_url, base_url)
        if not url or _is_junk_image_url(url):
            return
        norm_key = url.lower().rstrip('/').split('?')[0]
        if norm_key in seen:
            return
        seen.add(norm_key)
        images.append(url)

    # 1. og:image
    for pattern in [
        r'<meta[^>]+property=["\x27]og:image["\x27][^>]+content=["\x27]([^"\x27]+)["\x27]',
        r'<meta[^>]+content=["\x27]([^"\x27]+)["\x27][^>]+property=["\x27]og:image["\x27]',
        r'<meta[^>]+property=["\x27]og:image:url["\x27][^>]+content=["\x27]([^"\x27]+)["\x27]',
        r'<meta[^>]+property=["\x27]og:image:secure_url["\x27][^>]+content=["\x27]([^"\x27]+)["\x27]',
    ]:
        for m in re.finditer(pattern, html, re.IGNORECASE):
            _add(m.group(1))

    # 2. twitter:image
    for pattern in [
        r'<meta[^>]+name=["\x27]twitter:image["\x27][^>]+content=["\x27]([^"\x27]+)["\x27]',
        r'<meta[^>]+content=["\x27]([^"\x27]+)["\x27][^>]+name=["\x27]twitter:image["\x27]',
    ]:
        for m in re.finditer(pattern, html, re.IGNORECASE):
            _add(m.group(1))

    # 3. JSON-LD images
    for match in re.finditer(
        r'<script[^>]+type=["\x27]application/ld\+json["\x27][^>]*>(.*?)</script>',
        html, re.IGNORECASE | re.DOTALL
    ):
        try:
            import json
            data = json.loads(match.group(1))
            _extract_jsonld_images_recursive(data, _add)
        except Exception:
            pass

    # 4. <picture>/<source srcset>
    picture_blocks = re.findall(r'<picture[^>]*>(.*?)</picture>', html, re.IGNORECASE | re.DOTALL)
    for block in picture_blocks:
        for srcset in re.findall(r'srcset=["\x27]([^"\x27]+)["\x27]', block, re.IGNORECASE):
            for part in srcset.split(','):
                u = part.strip().split()[0] if part.strip() else ''
                _add(u)

    # 5. <img> tags — data-src first (lazy-loaded), then src
    for attr in ['data-src', 'data-lazy-src', 'data-original', 'src']:
        for m in re.finditer(
            rf'<img[^>]+{attr}=["\x27]([^"\x27]+)["\x27]', html, re.IGNORECASE
        ):
            _add(m.group(1))

    return images[:MAX_IMAGES_FROM_ARTICLE]


def _extract_jsonld_images_recursive(data, callback):
    """Recursively extract image URLs from JSON-LD structured data."""
    if isinstance(data, dict):
        # Direct image field
        image = data.get('image')
        if isinstance(image, str):
            callback(image)
        elif isinstance(image, dict):
            for key in ['url', 'contentUrl', '@id']:
                if isinstance(image.get(key), str):
                    callback(image[key])
        elif isinstance(image, list):
            for item in image:
                _extract_jsonld_images_recursive(item, callback)
        
        # Recurse into nested objects
        for key in ['@graph', 'itemListElement', 'hasPart']:
            if isinstance(data.get(key), list):
                for item in data[key]:
                    _extract_jsonld_images_recursive(item, callback)
    elif isinstance(data, list):
        for item in data:
            _extract_jsonld_images_recursive(item, callback)


# ── Main fetcher ──────────────────────────────────────────────────────────────

class ArticleResult:
    """Result of fetching and parsing an article page."""
    __slots__ = ('url', 'resolved_url', 'title', 'text', 'image_urls', 'lang')
    
    def __init__(self):
        self.url: str = ""
        self.resolved_url: str = ""
        self.title: str = ""
        self.text: str = ""
        self.image_urls: List[str] = []
        self.lang: str = ""  # Detected language
    
    def to_dict(self) -> Dict:
        return {
            "url": self.url,
            "resolved_url": self.resolved_url,
            "title": self.title,
            "text": self.text,
            "image_urls": self.image_urls,
            "lang": self.lang,
        }


async def fetch_article(url: str, client: Optional[httpx.AsyncClient] = None) -> Optional[ArticleResult]:
    """Fetch a full article page, extract text and images.
    
    Handles:
    - Google News redirect URLs → resolves to real article URL first
    - Regular article URLs → fetches and extracts directly
    - Returns ArticleResult with full text + image URLs
    
    Returns None only if the URL is completely unreachable.
    Even partial results (e.g., just images, no text) are returned.
    """
    if not url or not url.startswith(('http://', 'https://')):
        return None
    
    should_close = False
    if client is None:
        client = httpx.AsyncClient(
            timeout=FETCH_TIMEOUT,
            follow_redirects=True,
            headers=_BROWSER_HEADERS,
        )
        should_close = True
    
    result = ArticleResult()
    result.url = url
    
    try:
        # Step 1: Resolve Google News redirects
        resolved_url = url
        if _GOOGLE_NEWS_BASE_RE.search(url):
            resolved_url = await resolve_google_news_url(url, client)
            result.resolved_url = resolved_url
        else:
            result.resolved_url = url
        
        # Step 2: Fetch the article page
        fetch_url = resolved_url if resolved_url != url else url
        
        # Skip Google News URLs that couldn't be resolved
        if _GOOGLE_NEWS_BASE_RE.search(fetch_url):
            logger.info(f"Skipping unresolved Google News URL: {fetch_url[:60]}...")
            return None
        
        try:
            resp = await client.get(fetch_url, headers=_BROWSER_HEADERS, timeout=FETCH_TIMEOUT)
            if resp.status_code != 200:
                logger.debug(f"Article fetch returned {resp.status_code}: {fetch_url[:60]}...")
                return None
        except httpx.TimeoutException:
            logger.debug(f"Article fetch timeout: {fetch_url[:60]}...")
            return None
        except Exception as e:
            logger.debug(f"Article fetch error: {e}")
            return None
        
        html = resp.text
        base_url = str(resp.url)
        
        # Step 3: Extract title
        title_match = re.search(
            r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
            html, re.IGNORECASE
        )
        if title_match:
            result.title = html_unescape(title_match.group(1))
        else:
            # Fallback to <title> tag
            title_match = re.search(r'<title[^>]*>(.*?)</title>', html, re.IGNORECASE | re.DOTALL)
            if title_match:
                result.title = html_unescape(title_match.group(1).strip())
                # Clean up title (remove site name suffix)
                result.title = re.split(r'\s*[-–|·—]\s*', result.title)[0].strip()
        
        # Step 4: Detect language
        lang_match = re.search(
            r'<meta[^>]+property=["\']og:locale["\'][^>]+content=["\']([^"\']+)["\']',
            html, re.IGNORECASE
        )
        if lang_match:
            locale = lang_match.group(1).lower()
            if locale.startswith('ru'):
                result.lang = 'ru'
            elif locale.startswith('de'):
                result.lang = 'de'
            elif locale.startswith('en'):
                result.lang = 'en'
            else:
                result.lang = locale[:2]
        else:
            # Detect from HTML lang attribute
            html_lang = re.search(r'<html[^>]+lang=["\']([^"\']+)["\']', html, re.IGNORECASE)
            if html_lang:
                result.lang = html_lang.group(1).lower()[:2]
            else:
                # Detect from text content (Cyrillic = Russian)
                if any('\u0400' <= c <= '\u04FF' for c in result.title[:50]):
                    result.lang = 'ru'
                else:
                    result.lang = 'en'
        
        # Step 5: Extract article text
        result.text = _extract_article_text(html)
        
        # Step 6: Extract image URLs
        result.image_urls = extract_image_urls_from_html(html, base_url)
        
        logger.info(
            f"Article fetched: {len(result.text)} chars, {len(result.image_urls)} images "
            f"from {fetch_url[:50]}... (lang={result.lang})"
        )
        
        return result
    
    except Exception as e:
        logger.error(f"Article fetcher error for {url[:50]}...: {e}")
        return None
    finally:
        if should_close:
            await client.aclose()


async def enrich_news_item(item: Dict, client: Optional[httpx.AsyncClient] = None) -> Dict:
    """Enrich a news item with full article text and images.
    
    For items that have:
    - No images (image_urls is empty) — fetches the article page to get images
    - Short summary — fetches full article text for fact-gathering
    - Google News URL — resolves redirect and fetches real article
    
    The item dict is updated with:
    - image_urls: list of image URLs from the article page
    - full_text: full article text (for AI fact-gathering)
    - resolved_url: the real article URL (after Google News redirect resolution)
    - lang: detected language
    
    Returns the updated item dict.
    """
    article_url = item.get("url", "")
    existing_images = item.get("image_urls", [])
    summary = item.get("summary", "")
    
    # Skip if we already have enough images AND a good summary
    has_enough_images = len(existing_images) >= 3
    has_good_summary = len(summary) > 200
    
    if has_enough_images and has_good_summary:
        return item  # Already well-stocked
    
    # Skip AI-discovered items (no real URL)
    if article_url.startswith("ai_discovered_"):
        return item
    
    logger.info(
        f"Enriching news item: '{item.get('title', '')[:50]}' "
        f"(imgs={len(existing_images)}, summary={len(summary)} chars)"
    )
    
    try:
        result = await fetch_article(article_url, client)
        if result is None:
            return item
        
        # Update images (merge with existing, dedup)
        if result.image_urls:
            existing_set = set(url.lower().rstrip('/') for url in existing_images)
            for img_url in result.image_urls:
                norm_key = img_url.lower().rstrip('/')
                if norm_key not in existing_set:
                    existing_images.append(img_url)
                    existing_set.add(norm_key)
            item["image_urls"] = existing_images[:10]
        
        # Add full text for AI fact-gathering
        if result.text and len(result.text) > len(summary):
            item["full_text"] = result.text
        
        # Update resolved URL
        if result.resolved_url and result.resolved_url != article_url:
            item["resolved_url"] = result.resolved_url
        
        # Update language if detected
        if result.lang:
            item["lang"] = result.lang
        
        # Update title if we got a better one
        if result.title and len(result.title) > len(item.get("title", "")):
            item["title"] = result.title
        
        logger.info(
            f"Enriched: {len(item.get('image_urls', []))} images, "
            f"{len(item.get('full_text', ''))} chars text for '{item.get('title', '')[:40]}'"
        )
    
    except Exception as e:
        logger.debug(f"Article enrichment failed for '{item.get('title', '')[:40]}': {e}")
    
    return item


# ── Batch enrichment for news pipeline ────────────────────────────────────────

async def enrich_news_batch(items: List[Dict], max_concurrent: int = 3) -> List[Dict]:
    """Enrich a batch of news items with article text and images.
    
    Processes items concurrently (up to max_concurrent at a time).
    Only enriches items that need it (no images or short summary).
    Skips items that are already well-stocked.
    
    Returns the updated list of items.
    """
    import asyncio
    
    # Filter items that need enrichment
    need_enrichment = []
    for item in items:
        images = item.get("image_urls", [])
        summary = item.get("summary", "")
        url = item.get("url", "")
        # Needs enrichment if: no images, or short summary, or Google News URL
        needs = (
            len(images) < 3
            or len(summary) < 200
            or _GOOGLE_NEWS_BASE_RE.search(url)
        )
        # Skip AI-discovered items
        if url.startswith("ai_discovered_"):
            needs = False
        if needs:
            need_enrichment.append(item)
    
    if not need_enrichment:
        return items
    
    logger.info(f"Enriching {len(need_enrichment)}/{len(items)} items with article content")
    
    # Use a shared client for connection pooling
    async with httpx.AsyncClient(
        timeout=FETCH_TIMEOUT,
        follow_redirects=True,
        headers=_BROWSER_HEADERS,
        limits=httpx.Limits(max_connections=max_concurrent, max_keepalive_connections=max_concurrent),
    ) as client:
        # Process with semaphore for concurrency control
        semaphore = asyncio.Semaphore(max_concurrent)
        
        async def _enrich_with_semaphore(item):
            async with semaphore:
                return await enrich_news_item(item, client)
        
        # Run all enrichments concurrently
        tasks = [_enrich_with_semaphore(item) for item in need_enrichment]
        enriched = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Log results
        success = sum(1 for r in enriched if not isinstance(r, Exception))
        failed = sum(1 for r in enriched if isinstance(r, Exception))
        logger.info(f"Batch enrichment: {success} succeeded, {failed} failed")
    
    return items
