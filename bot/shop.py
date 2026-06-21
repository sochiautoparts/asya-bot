"""
Shop Catalog Manager v1.0
Fetches product catalog from sochiautoparts.ru/shop and prepares selections.

Architecture:
  - HTML scraping of /shop and /shop/category/<slug> pages
  - JSON-LD (schema.org/Product) parsing on individual product pages
  - SQLite cache (shop_products table) — refreshed daily, used for selections
  - Selections: pick N fresh (unposted) products from a category, sort by price/variety

This module is the data layer. Posting logic lives in channel.py
(post_product_selection method).

Integration with existing bot:
  - Same httpx/aiohttp patterns as news.py and bot/article_fetcher.py
  - Uses bot.database._connect_db for DB access
  - Logs under "asya.shop" namespace
"""

import asyncio
import json
import logging
import random
import re
from html import unescape as html_unescape
from typing import List, Dict, Optional, Tuple
from urllib.parse import quote

import httpx

from bot.config import config

logger = logging.getLogger("asya.shop")

# ── Shop URLs ──────────────────────────────────────────────────────────────────

SHOP_BASE = "https://sochiautoparts.ru"
SHOP_LISTING = SHOP_BASE + "/shop"
SHOP_CATEGORY = SHOP_BASE + "/shop/category/{slug}"
SHOP_PRODUCT = SHOP_BASE + "/shop/product/{slug}"

FETCH_TIMEOUT = 30.0
USER_AGENT = "AsyaBot/1.0 ShopFetcher (+https://sochiautoparts.ru)"

# ── Categories the bot rotates through for daily selections ───────────────────
# These are the high-traffic, high-margin categories that produce good-looking
# selections. URL slugs are URL-encoded Russian words.
# Curated list — we don't try to scrape ALL 36k products, just enough variety.

SHOP_CATEGORIES = [
    {"slug": "всесезонные",          "label": "Всесезонные шины",         "topic_hint": "шины,резина,колёса,колеса,всесезонка"},
    {"slug": "зимние",               "label": "Зимние шины",              "topic_hint": "зимние,зима,снег,лед,шиповка,липучка"},
    {"slug": "летние",               "label": "Летние шины",              "topic_hint": "летние,лето,асфальт,скорость"},
    {"slug": "автомобильные-диски",  "label": "Автомобильные диски",      "topic_hint": "диски,колёса,литые,кованые,штамповка"},
    {"slug": "автохимия",            "label": "Автохимия",                "topic_hint": "химия,масло,промывка,очиститель,антидождь"},
    {"slug": "стеклоомывающая-жидкость", "label": "Стеклоомывающая жидкость", "topic_hint": "омывайка,стекло,незамерзайка,жидкость"},
    {"slug": "мотоциклы",            "label": "Мотоциклы",                "topic_hint": "мото,мотоцикл,байк,эндуро,спортбайк"},
    {"slug": "квадроциклы",          "label": "Квадроциклы",              "topic_hint": "квадрик,квадроцикл,atv,вездеход"},
    {"slug": "снегоходы",            "label": "Снегоходы",                "topic_hint": "снегоход,зима,снег,буран"},
    {"slug": "лодочные-моторы",      "label": "Лодочные моторы",          "topic_hint": "мотор,лодка,яхта,катер,водохвод"},
    {"slug": "надувные-лодки-пвх",   "label": "Надувные лодки ПВХ",       "topic_hint": "лодка,пвх,надувная,рыбалка"},
    {"slug": "мотобуксировщики",     "label": "Мотобуксировщики",         "topic_hint": "мотособака,буксировщик,зима,снег"},
    {"slug": "снегоуборщики",        "label": "Снегоуборщики",            "topic_hint": "снегоуборщик,зима,двор,уборка"},
    {"slug": "беговые-дорожки",      "label": "Беговые дорожки",          "topic_hint": "бег,тренажёр,спорт,кардио"},
    {"slug": "автокосметика",        "label": "Автокосметика",            "topic_hint": "косметика,полироль,воск,блеск,кузов"},
]

# Map: keyword -> category slug. Used to match comment topics to shop categories.
_KEYWORD_TO_CATEGORY: Dict[str, str] = {}
for _c in SHOP_CATEGORIES:
    for _kw in _c["topic_hint"].split(","):
        _KEYWORD_TO_CATEGORY[_kw.strip().lower()] = _c["slug"]
del _c, _kw

# Map: lowercase JSON-LD category (e.g. "всесезонные") -> SHOP_CATEGORIES label
# so products stored in DB match the label used in queries.
# This handles the mismatch: JSON-LD says "Всесезонные" but our label is "Всесезонные шины".
_JSONLD_CATEGORY_TO_LABEL: Dict[str, str] = {}
for _c in SHOP_CATEGORIES:
    # Map both the slug and the first word of the label
    _JSONLD_CATEGORY_TO_LABEL[_c["slug"].lower()] = _c["label"]
    _first_word = _c["label"].split()[0].lower()
    if _first_word not in _JSONLD_CATEGORY_TO_LABEL:
        _JSONLD_CATEGORY_TO_LABEL[_first_word] = _c["label"]
del _c, _first_word


def normalize_category(jsonld_category: str) -> str:
    """Normalize a JSON-LD product category to a SHOP_CATEGORIES label.

    The shop's JSON-LD uses short category names like "Всесезонные", "Зимние",
    "Летние" — but our SHOP_CATEGORIES labels are "Всесезонные шины", "Зимние
    шины", etc. This function maps the short form to the full label so DB
    queries by category_label work correctly.

    Returns the normalized label, or the original input if no match.
    """
    if not jsonld_category:
        return ""
    cat_lower = jsonld_category.strip().lower()
    # Direct match by slug or first-word
    if cat_lower in _JSONLD_CATEGORY_TO_LABEL:
        return _JSONLD_CATEGORY_TO_LABEL[cat_lower]
    # Try first word of the JSON-LD category
    first_word = cat_lower.split()[0] if cat_lower.split() else cat_lower
    if first_word in _JSONLD_CATEGORY_TO_LABEL:
        return _JSONLD_CATEGORY_TO_LABEL[first_word]
    # No match — return original
    return jsonld_category.strip()


# ── Data classes ──────────────────────────────────────────────────────────────

class ProductCard:
    """Lightweight product info from a listing page (no affiliate URL yet)."""
    __slots__ = ("slug", "name", "image_url", "price_text", "supplier", "category_slug")

    def __init__(self, slug: str, name: str, image_url: str, price_text: str,
                 supplier: str, category_slug: str):
        self.slug = slug
        self.name = name
        self.image_url = image_url
        self.price_text = price_text
        self.supplier = supplier
        self.category_slug = category_slug


class Product:
    """Full product info from JSON-LD on the product page."""
    __slots__ = (
        "slug", "sku", "name", "brand", "category", "price", "currency",
        "image_url", "product_url", "affiliate_url", "description", "availability",
    )

    def __init__(self, slug: str, sku: str, name: str, brand: str, category: str,
                 price: float, currency: str, image_url: str, product_url: str,
                 affiliate_url: str, description: str, availability: str):
        self.slug = slug
        self.sku = sku
        self.name = name
        self.brand = brand
        self.category = category
        self.price = price
        self.currency = currency
        self.image_url = image_url
        self.product_url = product_url
        self.affiliate_url = affiliate_url
        self.description = description
        self.availability = availability

    def to_dict(self) -> Dict:
        return {k: getattr(self, k) for k in self.__slots__}


# ── HTTP fetcher ──────────────────────────────────────────────────────────────

async def _fetch_html(url: str, timeout: float = FETCH_TIMEOUT) -> Optional[str]:
    """Fetch a page, return HTML text or None on failure."""
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            },
        ) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                logger.debug(f"HTTP {resp.status_code} fetching {url[:80]}")
                return None
            return resp.text
    except Exception as e:
        logger.debug(f"Error fetching {url[:80]}: {e}")
        return None


# ── HTML parsing ──────────────────────────────────────────────────────────────

# Match a single <article class="shop-product-card">…</article> block
_PRODUCT_CARD_RE = re.compile(
    r'<article\s+class="shop-product-card">(.*?)</article>',
    re.DOTALL,
)
# Inside a card, extract href="/shop/product/<slug>"
_CARD_LINK_RE = re.compile(r'href="/shop/product/([^"]+)"')
# Inside a card, extract the first <img src="..." alt="...">
_CARD_IMG_RE = re.compile(
    r'<img[^>]+src="([^"]+)"[^>]+alt="([^"]*)"',
)
# Inside a card, extract <span class="price">…</span>
_CARD_PRICE_RE = re.compile(
    r'<span\s+class="price">([^<]+)</span>',
)
# Badge supplier (optional) — handle extra attributes (style, etc.) between class and >
_CARD_BADGE_RE = re.compile(
    r'<span\s+class="badge[^"]*"[^>]*>([^<]+)</span>',
)


def parse_listing_html(html: str, category_slug: str = "") -> List[ProductCard]:
    """Parse a /shop or /shop/category page. Returns up to 50 ProductCard objects."""
    cards: List[ProductCard] = []
    seen_slugs = set()

    for match in _PRODUCT_CARD_RE.finditer(html):
        block = match.group(1)
        link_m = _CARD_LINK_RE.search(block)
        if not link_m:
            continue
        slug = html_unescape(link_m.group(1))
        if slug in seen_slugs:
            continue
        seen_slugs.add(slug)

        img_m = _CARD_IMG_RE.search(block)
        image_url = img_m.group(1) if img_m else ""
        name = html_unescape(img_m.group(2)) if img_m else ""

        price_m = _CARD_PRICE_RE.search(block)
        price_text = html_unescape(price_m.group(1)).strip() if price_m else ""

        badge_m = _CARD_BADGE_RE.search(block)
        supplier = html_unescape(badge_m.group(1)).strip() if badge_m else ""

        if not name:
            # Fall back to slug-derived name
            name = slug.replace("-", " ")

        cards.append(ProductCard(
            slug=slug,
            name=name,
            image_url=image_url,
            price_text=price_text,
            supplier=supplier,
            category_slug=category_slug,
        ))

    return cards


# Match a JSON-LD <script type="application/ld+json"> block
_JSONLD_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL,
)


def parse_product_html(html: str, slug: str) -> Optional[Product]:
    """Parse a /shop/product/<slug> page using the JSON-LD schema.org/Product block."""
    for m in _JSONLD_RE.finditer(html):
        raw = m.group(1).strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        # Some pages embed a list (BreadcrumbList + Product). Find the Product.
        if isinstance(data, list):
            prod = next((d for d in data if isinstance(d, dict) and d.get("@type") == "Product"), None)
            if prod is None:
                continue
        elif isinstance(data, dict):
            if data.get("@type") != "Product":
                continue
            prod = data
        else:
            continue

        name = html_unescape(prod.get("name", "")).strip()
        sku = str(prod.get("sku", "") or slug)
        brand = ""
        brand_obj = prod.get("brand")
        if isinstance(brand_obj, dict):
            brand = html_unescape(brand_obj.get("name", "")).strip()
        elif isinstance(brand_obj, str):
            brand = brand_obj

        category = html_unescape(prod.get("category", "")).strip()
        # Normalize JSON-LD short category (e.g. "Всесезонные") to full
        # SHOP_CATEGORIES label (e.g. "Всесезонные шины") so DB queries match.
        category = normalize_category(category)
        description = html_unescape(prod.get("description", "")).strip()
        if len(description) > 500:
            description = description[:500].rsplit(" ", 1)[0] + "…"

        image_url = ""
        image = prod.get("image")
        if isinstance(image, list) and image:
            image_url = image[0]
        elif isinstance(image, str):
            image_url = image

        offers = prod.get("offers")
        affiliate_url = ""
        price = 0.0
        currency = "RUR"
        availability = "InStock"
        if isinstance(offers, dict):
            affiliate_url = offers.get("url", "") or ""
            try:
                price = float(offers.get("price", 0))
            except (TypeError, ValueError):
                price = 0.0
            currency = offers.get("priceCurrency", "RUR") or "RUR"
            availability = (offers.get("availability", "") or "").rsplit("/", 1)[-1] or "InStock"
        elif isinstance(offers, list) and offers:
            first = offers[0]
            if isinstance(first, dict):
                affiliate_url = first.get("url", "") or ""
                try:
                    price = float(first.get("price", 0))
                except (TypeError, ValueError):
                    price = 0.0
                currency = first.get("priceCurrency", "RUR") or "RUR"

        product_url = SHOP_PRODUCT.format(slug=quote(slug, safe=""))

        return Product(
            slug=slug,
            sku=sku,
            name=name,
            brand=brand,
            category=category,
            price=price,
            currency=currency,
            image_url=image_url,
            product_url=product_url,
            affiliate_url=affiliate_url,
            description=description,
            availability=availability,
        )

    return None


# ── Public API ────────────────────────────────────────────────────────────────

async def fetch_category_cards(slug: str, page: int = 1) -> List[ProductCard]:
    """Fetch a category listing page and return up to 50 ProductCards."""
    url = SHOP_CATEGORY.format(slug=quote(slug, safe=""))
    if page > 1:
        url += f"?page={page}"
    html = await _fetch_html(url)
    if not html:
        return []
    return parse_listing_html(html, category_slug=slug)


async def fetch_product(slug: str) -> Optional[Product]:
    """Fetch a single product page and return a Product (parsed from JSON-LD)."""
    url = SHOP_PRODUCT.format(slug=quote(slug, safe=""))
    html = await _fetch_html(url)
    if not html:
        return None
    return parse_product_html(html, slug=slug)


def category_for_text(text: str) -> Optional[Dict]:
    """Detect which shop category matches a free-text topic (e.g., a comment).

    Returns the category dict from SHOP_CATEGORIES, or None if no match.
    Used by comment_on_group_post to pick relevant products.
    """
    if not text:
        return None
    text_lower = text.lower()
    # Score each category by counting keyword hits in the text
    best_cat = None
    best_score = 0
    for cat in SHOP_CATEGORIES:
        score = 0
        for kw in cat["topic_hint"].split(","):
            kw = kw.strip().lower()
            if kw and kw in text_lower:
                score += 1
        if score > best_score:
            best_score = score
            best_cat = cat
    return best_cat if best_score > 0 else None


# ── Cache refresh ─────────────────────────────────────────────────────────────

async def refresh_category(slug: str, max_products: int = 25) -> int:
    """Fetch a category listing, then fetch each product page in parallel
    (bounded concurrency), and store results in the DB.

    Returns the number of NEW products added to the DB.
    """
    from bot.database import upsert_shop_product, get_shop_product_count_in_category

    cards = await fetch_category_cards(slug)
    if not cards:
        logger.info(f"shop: no cards fetched for category '{slug}'")
        return 0

    # If we already have enough products for this category, skip refresh
    existing = await get_shop_product_count_in_category(slug)
    if existing >= max_products * 2:
        logger.debug(f"shop: category '{slug}' already has {existing} products, skipping refresh")
        return 0

    # Limit how many we fetch (each fetch is an HTTP request)
    cards = cards[:max_products]
    logger.info(f"shop: refreshing {len(cards)} products in category '{slug}'")

    # Bounded concurrency: 5 parallel HTTP requests
    sem = asyncio.Semaphore(5)

    async def fetch_one(card: ProductCard) -> Optional[Product]:
        async with sem:
            return await fetch_product(card.slug)

    results = await asyncio.gather(*[fetch_one(c) for c in cards], return_exceptions=True)

    new_count = 0
    for r in results:
        if isinstance(r, Exception):
            logger.debug(f"shop: fetch error: {r}")
            continue
        if r is None:
            continue
        # Skip products without affiliate URL — useless for monetization
        if not r.affiliate_url:
            continue
        # Skip products without image — can't post in album
        if not r.image_url:
            continue
        try:
            added = await upsert_shop_product(r)
            if added:
                new_count += 1
        except Exception as e:
            logger.debug(f"shop: upsert error for {r.slug}: {e}")

    logger.info(f"shop: refresh '{slug}' done — {new_count} new products added")
    return new_count


async def refresh_random_category() -> Tuple[str, int]:
    """Pick a random category from SHOP_CATEGORIES and refresh it.
    Returns (slug, new_count).
    Used by the daily background refresher.
    """
    cat = random.choice(SHOP_CATEGORIES)
    new_count = await refresh_category(cat["slug"])
    return cat["slug"], new_count


async def refresh_all_categories_light(max_per_category: int = 10) -> Dict[str, int]:
    """Light refresh: fetch first page of every category, store only 10 products each.
    Used on first run when DB is empty.
    Returns {slug: new_count}.
    """
    results: Dict[str, int] = {}
    for cat in SHOP_CATEGORIES:
        try:
            n = await refresh_category(cat["slug"], max_products=max_per_category)
            results[cat["slug"]] = n
        except Exception as e:
            logger.warning(f"shop: refresh failed for '{cat['slug']}': {e}")
            results[cat["slug"]] = 0
    return results
