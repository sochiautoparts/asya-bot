"""Smart Content Engine v2.0 — Web-Search-First automotive content pipeline for @sochiautoparts.

ARCHITECTURE:
  Phase 1: WEB SEARCH — Primary source: fresh automotive news from web search
  Phase 2: SCORE & SELECT — AI interest scoring, pick top candidates
  Phase 3: AI PICK — AI selects the BEST topic from top 5 candidates
  Phase 4: RSS FALLBACK — Supplement when web search yields nothing good
  Phase 5: DEDUPLICATE — Persistent topic registry with entity extraction
  Phase 6: ENRICH — AI-powered deep content with expert opinion
  Phase 7: IMAGE — Multi-strategy image sourcing with web search
  Phase 8: POST — Quality validation with interest scoring

KEY FEATURES:
  - WEB SEARCH FIRST — always starts with fresh web search results
  - 20+ search queries in rotation (RU + EN) for broad coverage
  - Russian-specific market coverage (АвтоВАЗ, LADA, ГАЗ, Sochi region)
  - AI picks the BEST topic from top 5 — ensures most interesting content
  - RSS as fallback/supplement, not primary source
  - Topic registry prevents duplicate coverage of same event
  - AI interest scoring — skip boring/technical news nobody reads
  - Entity extraction — brand, model, event tracking for smart dedup
  - Date context — Ася always knows what year it is!
  - Multi-strategy images — RSS → scrape → web search → AI generation
"""

import logging
import re
import time
import random
import hashlib
import asyncio
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from bot.config import config, persona
from ai.router import ai_router
from bot.web_search import web_search, search_news

logger = logging.getLogger("asya.content_engine")

# Moscow timezone
_MOSCOW_TZ = ZoneInfo("Europe/Moscow")

# ── Topic Registry — persistent dedup by entities/events ──────────────────────
# Maps: entity_key → {first_seen, last_posted, post_count, titles}
# Entity key = normalized brand + model + event (e.g., "bmw_m5_reveal")
_topic_registry: Dict[str, Dict] = {}
_REGISTRY_MAX_AGE_HOURS = 72  # Forget topics after 72 hours

# Auto brands for entity extraction
_AUTO_BRANDS = [
    "BMW", "Mercedes", "Audi", "Volkswagen", "Toyota", "Honda", "Nissan",
    "Mazda", "Subaru", "Hyundai", "Kia", "Ford", "Chevrolet", "GMC",
    "Porsche", "Lexus", "Volvo", "Tesla", "BYD", "Zeekr", "Li Auto",
    "NIO", "Chery", "Haval", "Geely", "Changan", "Exeed", "Tank",
    "Lada", "ВАЗ", "Renault", "Peugeot", "Citroen", "Fiat", "Alfa Romeo",
    "Jaguar", "Land Rover", "Mini", "Smart", "Suzuki", "Mitsubishi",
    "Infiniti", "Acura", "Genesis", "Rivian", "Lucid", "Polestar",
    "Maserati", "Ferrari", "Lamborghini", "Bentley", "Rolls-Royce",
    "Bugatti", "McLaren", "Aston Martin", "Lotus",
]

# Event keywords for entity extraction
_EVENT_KEYWORDS = [
    "reveal", "launch", "debut", "unveil", "release", "announce",
    "премьера", "запуск", "дебют", "анонс", "представлен", "выпуск",
    "recalls", "отзыв", "ban", "запрет", "record", "рекорд",
    "crash", "авария", "merger", "слияни", "bankruptcy", "банкрот",
    "redesign", "рестайлинг", "facelift", "update", "обновлен",
    "discontinue", "снят", "сняти", "spy", "шпионск", "prototype", "прототип",
]


def _extract_entities(title: str) -> str:
    """Extract key entities from a news title for dedup.
    
    Returns a normalized entity key like "bmw_m5_reveal" or "toyota_recalls".
    This allows us to detect that "BMW M5 2027 revealed" and "BMW unveils new M5"
    are about the SAME event and should not be posted twice.
    """
    title_lower = title.lower()
    
    # Extract brand
    brand = ""
    for b in _AUTO_BRANDS:
        if b.lower() in title_lower:
            brand = b.lower()
            break
    
    # Extract model (alphanumeric after brand)
    model = ""
    if brand:
        # Common model patterns: M3, M5, X5, Q7, A4, E-Class, 911, etc.
        model_patterns = [
            r'\b([mglxqsec]\d+)\b',  # M3, X5, Q7, A4, E300, C-Class
            r'\b(\d{3,4}[ix]?)\b',   # 911, 330i, 540ix
            r'\b(class|series|corolla|camry|civic|accord|model\s?[s3xy])\b',
            r'\b(mustang|camaro|corvette|prius|rav4|highlander|pilot|tahoe)\b',
            r'\b(supra|gr86|brz|miata|wrangler|bronco|range rover|defender)\b',
            r'\b(taycan|macan|cayenne|panamera|911|718|boxster)\b',
        ]
        for pattern in model_patterns:
            match = re.search(pattern, title_lower)
            if match:
                model = match.group(1).replace(" ", "_")
                break
    
    # Extract event type
    event = ""
    for e in _EVENT_KEYWORDS:
        if e in title_lower:
            event = e
            break
    
    parts = [p for p in [brand, model, event] if p]
    return "_".join(parts) if parts else ""


def _is_topic_covered(entity_key: str) -> bool:
    """Check if this topic/entity was already posted about recently."""
    if not entity_key:
        return False
    
    entry = _topic_registry.get(entity_key)
    if not entry:
        return False
    
    # Check age
    age_hours = (time.time() - entry["last_posted"]) / 3600
    if age_hours > _REGISTRY_MAX_AGE_HOURS:
        # Topic too old — allow again
        del _topic_registry[entity_key]
        return False
    
    # Topic was posted in last 72h — it's covered
    return True


def _register_topic(entity_key: str, title: str):
    """Register that a topic was posted about.
    
    Also persists to DB so the registry survives restarts.
    """
    if not entity_key:
        return
    
    now = time.time()
    if entity_key in _topic_registry:
        _topic_registry[entity_key]["post_count"] += 1
        _topic_registry[entity_key]["last_posted"] = now
        _topic_registry[entity_key]["titles"].append(title)
    else:
        _topic_registry[entity_key] = {
            "first_seen": now,
            "last_posted": now,
            "post_count": 1,
            "titles": [title],
        }
    
    # Persist to DB (async, fire-and-forget via background task)
    try:
        entry = _topic_registry[entity_key]
        import asyncio
        from bot.database import save_topic_to_registry
        asyncio.create_task(save_topic_to_registry(
            entity_key=entity_key,
            first_seen=entry["first_seen"],
            last_posted=entry["last_posted"],
            post_count=entry["post_count"],
            titles=entry["titles"],
        ))
    except Exception as e:
        logger.debug(f"Could not persist topic to DB: {e}")


def _cleanup_registry():
    """Remove old entries from topic registry (in-memory and DB)."""
    now = time.time()
    max_age = _REGISTRY_MAX_AGE_HOURS * 3600
    expired = [k for k, v in _topic_registry.items() if now - v["last_posted"] > max_age]
    for k in expired:
        del _topic_registry[k]
    if expired:
        logger.info(f"Cleaned {len(expired)} expired topics from registry")
        # Also clean DB
        try:
            import asyncio
            from bot.database import cleanup_topic_registry
            asyncio.create_task(cleanup_topic_registry(_REGISTRY_MAX_AGE_HOURS))
        except Exception:
            pass


# ── Interest Scoring — rate how interesting a news item is ────────────────────

_HIGH_INTEREST_KEYWORDS = [
    # Breaking/big news
    "reveal", "debut", "launch", "unveil", "first", "новинка", "премьера",
    "рекорд", "record", "breakthrough", "прорыв",
    "дебют", "скандал", "отзыв", "ban", "recall", "revolutionary",
    # Popular brands
    "BMW M", "Mercedes AMG", "Porsche", "Ferrari", "Lamborghini",
    "Tesla", "Cybertruck", "Corvette", "Mustang", "Supra",
    # Russian-specific brands & market
    "АвтоВАЗ", "LADA", "ГАЗ", "УАЗ", "КамАЗ", "Соллерс",
    "Веста", "Granta", "Niva", "Vesta",
    # Popular topics
    "electric", "EV", "электромобиль", "электрокар", "autonomous", "беспилот",
    "recalls", "отзыв", "бан", "ban", "скандал", "scandal",
    "цена", "price", "стоимость", "стоить",
    # EV/transition topics
    "зарядная станция", "батарея", "battery", "электрокар",
    "plug-in", "зарядк", "range anxiety", "запас хода",
    # Engagement hooks
    "лучший", "худший", "самый", "worst", "best", "топ",
    "секрет", "secret", "тайн", "hidden",
]

_MEDIUM_INTEREST_KEYWORDS = [
    "update", "redesign", "обновлен", "рестайлинг",
    "test", "обзор", "тест-драйв", "review",
    "concept", "концепт", "prototype", "прототип",
    "hybrid", "гибрид", "plug-in", "PHEV",
    "новый", "new", "next-gen", "следующ",
    "мощност", "horsepower", "speed", "скорост",
    "двигатель", "engine", "turbo", "турбо",
    "автосалон", "auto show", "мотор-шоу", "motor show",
    "продаж", "sales", "рынок", "market",
    "китайск", "Chinese", "BYD", "Zeekr", "Haval", "Chery",
]

_LOW_INTEREST_KEYWORDS = [
    "report", "отчет", "statistics", "статистик",
    "regulation", "регуляц", "standard", "стандарт",
    "supplier", "поставщик", "factory", "завод",
    "share", "акци", "stock", "investor",
]


def _score_interest(title: str, summary: str = "") -> float:
    """Rate how interesting a news item is on a 0-1 scale.
    
    Higher scores = more interesting = more likely to engage readers.
    """
    text = f"{title} {summary}".lower()
    score = 0.5  # Base score
    
    # Boost for high-interest keywords
    for kw in _HIGH_INTEREST_KEYWORDS:
        if kw.lower() in text:
            score += 0.15
            break  # Only count once
    
    # Smaller boost for medium-interest keywords
    medium_count = sum(1 for kw in _MEDIUM_INTEREST_KEYWORDS if kw.lower() in text)
    score += min(medium_count * 0.05, 0.15)
    
    # Penalty for low-interest keywords
    for kw in _LOW_INTEREST_KEYWORDS:
        if kw.lower() in text:
            score -= 0.1
            break
    
    # Penalty for very long/technical titles
    if len(title) > 120:
        score -= 0.1
    
    # Bonus for brand names (people search by brand)
    for brand in _AUTO_BRANDS:
        if brand.lower() in text:
            score += 0.05
            break
    
    # Bonus for Russian-specific market topics
    russian_market_kw = ["автоваз", "lada", "газ", "уаз", "камаз", "соллерс",
                         "веста", "granta", "niva", "российск", "россия",
                         "сочи", "краснодар"]
    for kw in russian_market_kw:
        if kw in text:
            score += 0.08
            break
    
    # Bonus for EV/transition topics
    ev_kw = ["электромобиль", "электрокар", "зарядн", "ev", "батарея",
             "battery", "electric vehicle", "запас хода"]
    for kw in ev_kw:
        if kw in text:
            score += 0.08
            break
    
    return max(0.1, min(1.0, score))


# ── Web Search Content — supplement RSS with search results ───────────────────

_SEARCH_QUERIES_ROTATION = [
    # ── Russian-language queries (broad coverage) ──
    "автомобильные новости сегодня",
    "новые автомобили {year} премьера",
    "автоновости Россия",
    "новые модели авто {year}",
    "автомобильные новости сегодня {year}",
    "автопром России новости",
    "новинки авто {year} дебют",
    # ── English-language queries (international coverage) ──
    "automotive news today",
    "new car launches {year}",
    "electric vehicle news",
    "car industry updates",
    "new car models {year} reveal",
    "car recalls and safety {year}",
    "auto show reveals {year}",
    "automotive industry news today",
    "electric vehicle updates {year}",
    # ── Brand-specific queries (rotated) ──
    "LADA ВАЗ новости {year}",
    "Tesla news latest",
    "BMW Mercedes news latest",
    "BYD Chinese cars news",
]

# Track recently used query indices to avoid repetition
_recent_query_indices: list = []
_MAX_RECENT_QUERIES = 5


def _get_search_query() -> str:
    """Get a search query avoiding recent repetition."""
    global _recent_query_indices
    year = datetime.now(_MOSCOW_TZ).year
    
    # Pick a query index not recently used
    available = [i for i in range(len(_SEARCH_QUERIES_ROTATION)) if i not in _recent_query_indices]
    if not available:
        # All queries recently used — reset tracking
        _recent_query_indices = []
        available = list(range(len(_SEARCH_QUERIES_ROTATION)))
    
    idx = random.choice(available)
    _recent_query_indices.append(idx)
    if len(_recent_query_indices) > _MAX_RECENT_QUERIES:
        _recent_query_indices = _recent_query_indices[-_MAX_RECENT_QUERIES:]
    
    query = _SEARCH_QUERIES_ROTATION[idx]
    return query.format(year=year)


async def search_auto_news() -> List[Dict]:
    """Search the web for fresh automotive news using MULTIPLE queries for broad coverage.
    
    Uses 3 different queries per call (rotated from 20+ pool) to maximize coverage.
    Returns list of news items with: title, url, summary, source, category, lang, image_urls
    """
    items = []
    seen_urls = set()
    
    # Use 3 different queries per call for broader coverage
    queries = [_get_search_query() for _ in range(3)]
    # Deduplicate queries (in case same one picked twice)
    queries = list(dict.fromkeys(queries))
    
    for query in queries:
        logger.info(f"Searching web for auto news: {query}")
        try:
            results = await search_news(query, max_results=8)
            for result in results:
                title = result.title or ""
                url = result.url or ""
                snippet = result.snippet or ""
                
                if not title or not url:
                    continue
                
                # Dedup by URL
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                
                # Detect language from title content
                is_russian = any('\u0400' <= c <= '\u04FF' for c in title)
                
                items.append({
                    "source": result.source or "web_search",
                    "title": title.strip(),
                    "url": url.strip(),
                    "summary": snippet.strip()[:500],
                    "published": time.time(),
                    "category": "auto",
                    "lang": "ru" if is_russian else "en",
                    "image_urls": [],  # Will be filled by image pipeline
                })
        except Exception as e:
            logger.error(f"Web search for auto news failed (query: {query}): {e}")
    
    logger.info(f"Web search found {len(items)} auto news items across {len(queries)} queries")
    return items


async def search_russian_auto_news() -> List[Dict]:
    """Search for Russian-specific automotive market news.
    
    Specific searches for:
    - sochiautoparts.ru relevant content
    - Russian automotive brands (АвтоВАЗ, LADA, ГАЗ, УАЗ, КамАЗ)
    - Sochi/Krasnodar region auto news
    - Russian car market updates
    
    Returns list of news items.
    """
    items = []
    seen_urls = set()
    
    russian_queries = [
        "АвтоВАЗ LADA новости сегодня",
        "УАЗ ГАЗ КамАЗ новости {year}",
        "автоновости Сочи Краснодар",
        "автомобильный рынок Россия {year}",
        "sochiautoparts.ru новости авто",
        "Российский автопром новости",
        "LADA Веста Гранта Нива новости",
        "Соллерс автомобили новости",
    ]
    
    year = datetime.now(_MOSCOW_TZ).year
    # Pick 2 queries per call to not overload search
    selected = random.sample(russian_queries, min(2, len(russian_queries)))
    
    for query_template in selected:
        query = query_template.format(year=year)
        logger.info(f"Searching Russian auto news: {query}")
        try:
            results = await search_news(query, max_results=5)
            for result in results:
                title = result.title or ""
                url = result.url or ""
                snippet = result.snippet or ""
                
                if not title or not url:
                    continue
                
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                
                items.append({
                    "source": result.source or "web_search_ru",
                    "title": title.strip(),
                    "url": url.strip(),
                    "summary": snippet.strip()[:500],
                    "published": time.time(),
                    "category": "auto",
                    "lang": "ru",
                    "image_urls": [],
                })
        except Exception as e:
            logger.error(f"Russian auto news search failed (query: {query}): {e}")
    
    logger.info(f"Russian auto news search found {len(items)} items")
    return items


# ── Smart Image Search — find images via web search ───────────────────────────

async def search_news_images(query: str, max_count: int = 2) -> List[str]:
    """Search the web for images related to a news topic.
    
    Returns list of image URLs.
    """
    image_urls = []
    try:
        # Search for images
        search_query = f"{query} car photo"
        results = await web_search(search_query, num_results=5)
        for result in results:
            url = result.get("url", "")
            # Check if it looks like an image URL
            if any(ext in url.lower() for ext in ['.jpg', '.jpeg', '.png', '.webp']):
                if len(url) > 50:  # Skip tiny/tracking URLs
                    image_urls.append(url)
            # Also check snippet for image URLs
            snippet = result.get("snippet", "")
            img_match = re.search(r'https?://\S+\.(?:jpg|jpeg|png|webp)', snippet, re.IGNORECASE)
            if img_match:
                image_urls.append(img_match.group(0))
        
    except Exception as e:
        logger.debug(f"Image search failed for '{query}': {e}")
    
    return image_urls[:max_count]


# ── Main Content Pipeline ────────────────────────────────────────────────────

async def get_best_news_item(unposted_items: List[Dict]) -> Optional[Dict]:
    """Select the best news item to post — WEB SEARCH FIRST pipeline.
    
    Pipeline (v2 — Web-Search-First):
    1. Web search for fresh automotive news (PRIMARY source)
    2. Score all items (web search + any RSS unposted) for interest
    3. Extract entities and check topic registry
    4. Filter out covered topics
    5. Check channel scanner for already-posted topics
    6. Take top 5 candidates → AI picks the BEST one
    7. RSS as fallback when web search yields nothing good
    """
    # Cleanup old registry entries
    _cleanup_registry()
    
    # Load persisted registry from DB if empty (first call after restart)
    global _topic_registry
    if not _topic_registry:
        try:
            from bot.database import load_topic_registry
            _topic_registry = await load_topic_registry()
            if _topic_registry:
                logger.info(f"Loaded {len(_topic_registry)} topics from DB registry")
        except Exception as e:
            logger.debug(f"Could not load topic registry from DB: {e}")
    
    all_items = []
    
    # ── PHASE 1: Web Search FIRST (primary source) ──
    logger.info("Phase 1: Searching web for automotive news (PRIMARY)")
    try:
        web_items = await search_auto_news()
        all_items.extend(web_items)
        logger.info(f"Web search provided {len(web_items)} items")
    except Exception as e:
        logger.warning(f"Web search failed: {e}")
    
    # Also search Russian-specific news
    try:
        ru_items = await search_russian_auto_news()
        all_items.extend(ru_items)
        logger.info(f"Russian auto search provided {len(ru_items)} items")
    except Exception as e:
        logger.warning(f"Russian auto news search failed: {e}")
    
    # ── PHASE 2: Add RSS unposted items as supplement ──
    if unposted_items:
        all_items.extend(unposted_items)
        logger.info(f"Added {len(unposted_items)} RSS unposted items as supplement")
    
    # ── PHASE 3: Score, dedup, and filter ──
    scored_items = []
    seen_titles = set()
    
    # Load channel posts for scanner dedup
    channel_posts_set = set()
    try:
        from bot.channel_scanner import fetch_channel_posts
        channel_posts = await fetch_channel_posts(max_posts=50)
        for post in channel_posts:
            # Extract key words from each channel post for quick matching
            words = set(re.findall(r'[a-zа-яё]{3,}', post.lower()))
            channel_posts_set.add(frozenset(words))
    except Exception as e:
        logger.debug(f"Could not fetch channel posts for dedup: {e}")
    
    for item in all_items:
        title = item.get("title", "")
        summary = item.get("summary", "")
        
        # Skip exact title duplicates
        title_lower = title.lower().strip()
        if title_lower in seen_titles:
            continue
        seen_titles.add(title_lower)
        
        # Extract entities for dedup
        entity_key = _extract_entities(title)
        
        # Check if topic is already covered (in-memory + DB registry)
        if _is_topic_covered(entity_key):
            logger.debug(f"Topic already covered: {entity_key} — {title[:50]}")
            continue
        
        # Check if topic is already in the channel (channel scanner dedup)
        is_channel_dup = False
        if channel_posts_set:
            title_words = set(re.findall(r'[a-zа-яё]{3,}', title.lower()))
            if title_words:
                for channel_words in channel_posts_set:
                    overlap = title_words & channel_words
                    if len(overlap) >= 4 and len(overlap) / max(len(title_words), 1) >= 0.35:
                        logger.debug(f"Channel dedup in content engine: {title[:50]}")
                        # Register as covered so we don't see it again
                        _register_topic(entity_key, title)
                        is_channel_dup = True
                        break
        if is_channel_dup:
            continue  # Skip this item (it's a duplicate in the channel)
        
        # Score interest
        interest = _score_interest(title, summary)
        
        scored_items.append({
            "item": item,
            "interest": interest,
            "entity_key": entity_key,
        })
    
    # ── PHASE 4: RSS FALLBACK — if web search yielded nothing good ──
    if not scored_items:
        logger.info("Web search + RSS yielded no fresh topics — trying RSS-only fallback")
        # Try getting unposted items directly from DB (if not already provided)
        if not unposted_items:
            try:
                from bot.database import get_unposted_news
                fallback_items = await get_unposted_news(limit=10)
                for item in fallback_items:
                    title = item.get("title", "")
                    entity_key = _extract_entities(title)
                    if not _is_topic_covered(entity_key):
                        interest = _score_interest(title, item.get("summary", ""))
                        scored_items.append({
                            "item": item,
                            "interest": interest,
                            "entity_key": entity_key,
                        })
            except Exception as e:
                logger.warning(f"RSS fallback failed: {e}")
        
        if not scored_items:
            logger.info("No fresh topics from any source — skipping this cycle")
            return None
    
    # Sort by interest score (highest first)
    scored_items.sort(key=lambda x: x["interest"], reverse=True)
    
    # ── PHASE 5: AI picks the BEST from top 5 ──
    top_n = scored_items[:min(5, len(scored_items))]
    
    if len(top_n) == 1:
        chosen = top_n[0]
    else:
        # Let AI pick the most interesting one from top 5
        try:
            candidates_summary = []
            for i, entry in enumerate(top_n):
                item = entry["item"]
                candidates_summary.append(
                    f"{i+1}. [{entry['interest']:.2f}] {item.get('title', '')[:100]}"
                )
            
            candidates_text = "\n".join(candidates_summary)
            response = await ai_router._primary.chat(
                messages=[
                    {"role": "system", "content": (
                        "Ты редактор автоканала в Telegram. Тебе даны 5 кандидатов на публикацию "
                        "с оценкой интереса (0-1). Выбери САМЫЙ интересный для широкой аудитории — "
                        "то, что вызовет наибольший отклик, обсуждение и репосты. "
                        "Учитывай: премьеры, скандалы, рекорды, прорывы, российский рынок — "
                        "всегда приоритетнее сухих новостей. "
                        "Ответь ТОЛЬКО цифрой (1-5) — номер лучшего кандидата."
                    )},
                    {"role": "user", "content": f"Кандидаты:\n{candidates_text}"},
                ],
                model="openai-fast",
                temperature=0.3,
                max_tokens=5,
            )
            
            if not response.error and response.text:
                pick_str = response.text.strip()
                # Extract number from response
                pick_match = re.search(r'[1-5]', pick_str)
                if pick_match:
                    pick_idx = int(pick_match.group()) - 1
                    if 0 <= pick_idx < len(top_n):
                        chosen = top_n[pick_idx]
                        logger.info(f"AI picked candidate #{pick_idx + 1}: {chosen['item'].get('title', '')[:60]}")
                    else:
                        chosen = top_n[0]
                else:
                    chosen = top_n[0]
            else:
                chosen = top_n[0]
        except Exception as e:
            logger.debug(f"AI topic selection failed, using top-1: {e}")
            chosen = top_n[0]
    
    best_item = chosen["item"]
    entity_key = chosen["entity_key"]
    interest_score = chosen["interest"]
    
    logger.info(
        f"Selected news: interest={interest_score:.2f}, entity={entity_key or 'none'}, "
        f"title={best_item.get('title', '')[:60]}"
    )
    
    # Register the topic
    _register_topic(entity_key, best_item.get("title", ""))
    
    return best_item


async def enrich_with_search_images(news_item: Dict) -> List[str]:
    """Enrich a news item with images found via web search.
    
    This is an additional image source beyond RSS and article scraping.
    Searches for the car brand/model mentioned in the title.
    """
    title = news_item.get("title", "")
    
    # Extract a search-friendly query from the title
    # Remove common filler words
    query = re.sub(r'\b(the|a|an|in|on|at|to|for|of|with|and|or|but)\b', '', title, flags=re.IGNORECASE)
    query = re.sub(r'\s+', ' ', query).strip()
    # Keep first 60 chars for search
    query = query[:60]
    
    try:
        image_urls = await search_news_images(query, max_count=2)
        if image_urls:
            logger.info(f"Found {len(image_urls)} web search images for: {title[:50]}")
        return image_urls
    except Exception as e:
        logger.debug(f"Search image enrichment failed: {e}")
        return []


def get_date_context() -> str:
    """Get current date/time context string for AI prompts.
    
    Returns something like: 'Сейчас пятница, 6 июня 2026 года, время 14:30 МСК.'
    """
    now = datetime.now(_MOSCOW_TZ)
    days_ru = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    months_ru = ["января", "февраля", "марта", "апреля", "мая", "июня",
                 "июля", "августа", "сентября", "октября", "ноября", "декабря"]
    return (
        f"Сейчас {days_ru[now.weekday()]}, {now.day} {months_ru[now.month - 1]} "
        f"{now.year} года, время {now.strftime('%H:%M')} МСК."
    )


def get_registry_stats() -> Dict:
    """Get topic registry statistics for monitoring."""
    return {
        "total_topics": len(_topic_registry),
        "topics": {k: {"post_count": v["post_count"], "age_hours": round((time.time() - v["first_seen"]) / 3600, 1)}
                   for k, v in list(_topic_registry.items())[:20]},
    }
