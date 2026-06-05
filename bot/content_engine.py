"""Smart Content Engine v1.0 — Intelligent automotive content pipeline for @sochiautoparts.

ARCHITECTURE:
  Phase 1: AGGREGATE — Multi-source collection (RSS + web search + trending topics)
  Phase 2: DEDUPLICATE — Persistent topic registry with entity extraction
  Phase 3: ENRICH — AI-powered deep content with expert opinion
  Phase 4: IMAGE — Multi-strategy image sourcing with web search
  Phase 5: POST — Quality validation with interest scoring

KEY FEATURES:
  - Web search supplements RSS — always finds fresh news
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
    """Register that a topic was posted about."""
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


def _cleanup_registry():
    """Remove old entries from topic registry."""
    now = time.time()
    max_age = _REGISTRY_MAX_AGE_HOURS * 3600
    expired = [k for k, v in _topic_registry.items() if now - v["last_posted"] > max_age]
    for k in expired:
        del _topic_registry[k]
    if expired:
        logger.info(f"Cleaned {len(expired)} expired topics from registry")


# ── Interest Scoring — rate how interesting a news item is ────────────────────

_HIGH_INTEREST_KEYWORDS = [
    # Breaking/big news
    "reveal", "debut", "launch", "unveil", "first", "новинка", "премьера",
    "рекорд", "record", "breakthrough", "прорыв",
    # Popular brands
    "BMW M", "Mercedes AMG", "Porsche", "Ferrari", "Lamborghini",
    "Tesla", "Cybertruck", "Corvette", "Mustang", "Supra",
    # Popular topics
    "electric", "EV", "электромобиль", "autonomous", "беспилот",
    "recalls", "отзыв", "бан", "ban", "скандал", "scandal",
    "цена", "price", "стоимость", "стоить",
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
    
    return max(0.1, min(1.0, score))


# ── Web Search Content — supplement RSS with search results ───────────────────

_SEARCH_QUERIES_ROTATION = [
    "automotive news today {year}",
    "car industry latest news",
    "new car models {year} reveal",
    "electric vehicle news latest",
    "auto show {year} news",
    "car recalls {year} latest",
    "BMW news latest",
    "Tesla news latest",
    "автомобильные новости сегодня",
    "новые автомобили {year} премьера",
]

def _get_search_query() -> str:
    """Get a weighted random search query with current year."""
    year = datetime.now(_MOSCOW_TZ).year
    query = random.choice(_SEARCH_QUERIES_ROTATION)
    return query.format(year=year)


async def search_auto_news() -> List[Dict]:
    """Search the web for fresh automotive news.
    
    Returns list of news items with: title, url, summary, source, category, lang, image_urls
    """
    query = _get_search_query()
    logger.info(f"Searching web for auto news: {query}")
    
    items = []
    try:
        results = await search_news(query, num_results=10)
        for result in results:
            title = result.get("name", "") or result.get("title", "")
            url = result.get("url", "")
            snippet = result.get("snippet", "") or result.get("description", "")
            
            if not title or not url:
                continue
            
            items.append({
                "source": result.get("host_name", "web_search"),
                "title": title.strip(),
                "url": url.strip(),
                "summary": snippet.strip()[:500],
                "published": time.time(),
                "category": "auto",
                "lang": "en",
                "image_urls": [],  # Will be filled by image pipeline
            })
    except Exception as e:
        logger.error(f"Web search for auto news failed: {e}")
    
    logger.info(f"Web search found {len(items)} auto news items")
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
    """Select the best news item to post based on interest scoring and dedup.
    
    Pipeline:
    1. Score each item for interest
    2. Extract entities and check topic registry
    3. Filter out covered topics
    4. Return the highest-scoring uncovered item
    
    If no good items from RSS, tries web search as fallback.
    """
    if not unposted_items:
        return None
    
    # Cleanup old registry entries
    _cleanup_registry()
    
    # Score and filter items
    scored_items = []
    for item in unposted_items:
        title = item.get("title", "")
        summary = item.get("summary", "")
        
        # Extract entities for dedup
        entity_key = _extract_entities(title)
        
        # Check if topic is already covered
        if _is_topic_covered(entity_key):
            logger.debug(f"Topic already covered: {entity_key} — {title[:50]}")
            continue
        
        # Score interest
        interest = _score_interest(title, summary)
        
        scored_items.append({
            "item": item,
            "interest": interest,
            "entity_key": entity_key,
        })
    
    if not scored_items:
        logger.info("All RSS items are covered topics or low interest — trying web search")
        # Try web search as fallback
        search_items = await search_auto_news()
        for item in search_items:
            title = item.get("title", "")
            entity_key = _extract_entities(title)
            if not _is_topic_covered(entity_key):
                interest = _score_interest(title, item.get("summary", ""))
                scored_items.append({
                    "item": item,
                    "interest": interest,
                    "entity_key": entity_key,
                })
        
        if not scored_items:
            logger.info("No fresh topics from web search either — skipping this cycle")
            return None
    
    # Sort by interest score (highest first)
    scored_items.sort(key=lambda x: x["interest"], reverse=True)
    
    # Pick from top 3 (with some randomness for variety)
    top_n = scored_items[:min(3, len(scored_items))]
    chosen = random.choice(top_n)
    
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
