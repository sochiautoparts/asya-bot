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
from urllib.parse import quote_plus

import httpx

from bot.config import config, persona
from ai.router import ai_router
from bot.web_search import web_search, search_news, search_google_news_rss

logger = logging.getLogger("asya.content_engine")

# ── Content Format Types for 24/7 schedule ─────────────────────────────────────
CONTENT_FORMATS = {
    "world_news": {"emoji": "🌍", "name": "Мировая новость", "priority": 1},
    "russian_news": {"emoji": "📰", "name": "Российская новость", "priority": 1},
    "part_of_day": {"emoji": "🔧", "name": "Запчасть дня", "priority": 2},
    "tech_fact": {"emoji": "🧠", "name": "Знаете ли вы?", "priority": 2},
    "global_poll": {"emoji": "💬", "name": "Глобальный опрос", "priority": 2},
    "asya_drive": {"emoji": "🏎️", "name": "Драйв Аси", "priority": 3},
    "garage_story": {"emoji": "🏠", "name": "Гаражная история", "priority": 3},
    "market_day": {"emoji": "🌐", "name": "Рынок дня", "priority": 3},
    "auto_lesson": {"emoji": "🎓", "name": "Автоурок", "priority": 3},
    "espresso": {"emoji": "☕", "name": "Эспрессо с Асей", "priority": 2},
    "legend": {"emoji": "🌙", "name": "Легенда дорог", "priority": 4},
    "fact_check": {"emoji": "🔍", "name": "Фактчек", "priority": 3},
    "night_tip": {"emoji": "🌙", "name": "Ночной совет", "priority": 4},
    "auto_fact": {"emoji": "🌙", "name": "Автофакт", "priority": 4},
    "morning_greeting": {"emoji": "☀️", "name": "Доброе утро!", "priority": 1},
    "editors_life": {"emoji": "😂", "name": "Жизнь редакции", "priority": 4},
}

# Moscow timezone
_MOSCOW_TZ = ZoneInfo("Europe/Moscow")

# ── Topic Registry — persistent dedup by entities/events ──────────────────────
# Maps: entity_key → {first_seen, last_posted, post_count, titles}
# Entity key = normalized brand + model + event (e.g., "bmw_m5_reveal")
_topic_registry: Dict[str, Dict] = {}
_REGISTRY_MAX_AGE_HOURS = 72  # Keep topics for 72 hours — prevents same news reappearing next day

# Auto brands for entity extraction
_AUTO_BRANDS = [
    "BMW", "Mercedes", "Audi", "Volkswagen", "Toyota", "Honda", "Nissan",
    "Mazda", "Subaru", "Hyundai", "Kia", "Ford", "Chevrolet", "GMC",
    "Porsche", "Lexus", "Volvo", "Tesla", "BYD", "Zeekr", "Li Auto",
    "NIO", "Chery", "Haval", "Geely", "Changan", "Exeed", "Tank",
    "Renault", "Peugeot", "Citroen", "Fiat", "Alfa Romeo",
    "Jaguar", "Land Rover", "Mini", "Smart", "Suzuki", "Mitsubishi",
    "Infiniti", "Acura", "Genesis", "Rivian", "Lucid", "Polestar",
    "Maserati", "Ferrari", "Lamborghini", "Bentley", "Rolls-Royce",
    "Bugatti", "McLaren", "Aston Martin", "Lotus",
]

# Notable people / F1 drivers for entity extraction (prevents re-posting same person's story)
_NOTABLE_PEOPLE = [
    "Alonso", "Hamilton", "Verstappen", "Vettel", "Leclerc", "Norris",
    "Sainz", "Russell", "Perez", "Piastri", "Ricciardo", "Stroll",
    "Ocon", "Gasly", "Tsunoda", "Albon", "Zhou", "Bottas", "Hulkenberg",
    "Senna", "Prost", "Schumacher", "Lauda", "Hunt", "Moss",
    "Musk", "Toyota CEO", "VW CEO", "Diess", "Zellmer",
    "Маск", "Шумахер", "Сенна",
]

# F1 / motorsport teams for entity extraction
_MOTORSPORT_TEAMS = [
    "Red Bull", "Ferrari", "Mercedes", "McLaren", "Aston Martin",
    "Alpine", "Williams", "Haas", "RB", "Sauber", "Kick Sauber",
    "F1", "Formula 1", "Формула 1", "WRC", "WEC", "Le Mans",
    "NASCAR", "IndyCar", "MotoGP",
]

# Event keywords for entity extraction
_EVENT_KEYWORDS = [
    "reveal", "launch", "debut", "unveil", "release", "announce",
    "премьера", "запуск", "дебют", "анонс", "представлен", "выпуск",
    "recalls", "отзыв", "ban", "запрет", "record", "рекорд",
    "crash", "авария", "merger", "слияни", "bankruptcy", "банкрот",
    "redesign", "рестайлинг", "facelift", "update", "обновлен",
    "discontinue", "снят", "сняти", "spy", "шпионск", "prototype", "прототип",
    # F1 driver names for entity extraction
    "alonso", "verstappen", "hamilton", "leclerc", "norris",
    "sainz", "perez", "vettel", "russell", "piastri",
]


def _extract_entities(title: str) -> str:
    """Extract key entities from a news title for dedup.
    
    Returns a normalized entity key like "bmw_m5_reveal", "alonso_criticism", or "toyota_recalls".
    This allows us to detect that "BMW M5 2027 revealed" and "BMW unveils new M5"
    are about the SAME event and should not be posted twice.
    Also detects notable people (F1 drivers, CEOs) and motorsport topics.
    """
    title_lower = title.lower()
    
    # Extract brand
    brand = ""
    for b in _AUTO_BRANDS:
        if b.lower() in title_lower:
            brand = b.lower().replace(" ", "_")
            break
    
    # Extract notable person (F1 driver, CEO, etc.)
    person = ""
    for p in _NOTABLE_PEOPLE:
        if p.lower() in title_lower:
            person = p.lower().replace(" ", "_")
            break
    
    # Extract motorsport team/series
    team = ""
    for t in _MOTORSPORT_TEAMS:
        if t.lower() in title_lower:
            team = t.lower().replace(" ", "_")
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
    
    parts = [p for p in [brand, person, team, model, event] if p]
    entity_key = "_".join(parts) if parts else ""
    
    # ── BRAND-ONLY DEDUP KEY ──
    # If a brand is mentioned without a specific model+event, also check brand-only coverage
    # This prevents posting 3 different stories about the same brand in one day
    
    # ── PERSON-ONLY DEDUP KEY ──
    # If a notable person is mentioned (e.g. "Alonso"), also register a person-only key
    # This prevents posting 3 different stories about Alonso in the same day
    if person and entity_key != person:
        # Register person-only topic as well (e.g., "alonso" besides "alonso_criticism")
        person_only_key = person
        if _is_topic_covered(person_only_key) and not entity_key:
            # Person was already posted about and we don't have a more specific key
            # This helps prevent "Alonso 3x" duplicates
            pass  # We still allow it if there's a more specific entity key
        # We'll register person_only_key separately when posting
    
    return entity_key


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
    
    Also registers person-only dedup key to prevent "Alonso 3x" style duplicates.
    Persists to DB so the registry survives restarts.
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
    
    # ── Also register brand-only key for dedup ──
    # Extract brand from entity_key (e.g., "bmw_x5_reveal" → "bmw")
    for b in _AUTO_BRANDS:
        b_key = b.lower().replace(" ", "_")
        if b_key in entity_key:
            if b_key not in _topic_registry:
                _topic_registry[b_key] = {
                    "first_seen": now,
                    "last_posted": now,
                    "post_count": 1,
                    "titles": [f"[brand-dedup] {title}"],
                }
            else:
                _topic_registry[b_key]["post_count"] += 1
                _topic_registry[b_key]["last_posted"] = now
            break
    
    # ── Also register person-only key for dedup ──
    # Extract person from entity_key (e.g., "alonso_criticism" → "alonso")
    for p in _NOTABLE_PEOPLE:
        p_key = p.lower().replace(" ", "_")
        if p_key in entity_key:
            if p_key not in _topic_registry:
                _topic_registry[p_key] = {
                    "first_seen": now,
                    "last_posted": now,
                    "post_count": 1,
                    "titles": [f"[person-dedup] {title}"],
                }
            else:
                _topic_registry[p_key]["post_count"] += 1
                _topic_registry[p_key]["last_posted"] = now
            break
    
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
    # Motorsport & racing
    "F1", "Formula 1", "Формула 1", "WRC", "rally", "ралли",
    "Le Mans", "NASCAR", "IndyCar", "MotoGP", "Dakar", "Дакар",
    "grand prix", "гонк", "автоспорт", "motorsport", "racing",
    # Chinese auto expansion
    "BYD", "Zeekr", "Li Auto", "NIO", "Xpeng", "Chery", "Haval",
    "китайск", "Chinese cars", "китайские авто",
    # Technology & innovation
    "автопилот", "self-driving", "водород", "hydrogen", "solid-state",
    "твердотельн", "автомат", "робот", "софт", "over-the-air", "OTA",
    # Lifestyle & culture
    "тюнинг", "tuning", "рестайлинг", "facelift", "classic car",
    "винтаж", "ретро", "коллекцион",
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
    # Service & maintenance
    "обслуживан", "maintenance", "замена", "replacement",
    "масло", "oil", "фильтр", "filter", "тормозн", "brake",
    "диагност", "diagnostic", "VIN", "запчаст",
    # Comparisons & ratings
    "сравнен", "comparison", "рейтинг", "rating", "топ", "top",
    "плюс", "минус", "pros", "cons", "преимуществ",
    # Market & industry
    "экспорт", "export", "импорт", "import", "производств",
    "завод", "factory", "investment", "инвестиц",
    "продажи", "sales figures", "дилер", "dealer",
    # Lifestyle
    "путешеств", "road trip", "дорог", "маршрут",
    "безопасност", "safety", "страхов",
]

_LOW_INTEREST_KEYWORDS = [
    "report", "отчет", "statistics", "статистик",
    "regulation", "регуляц", "standard", "стандарт",
    "supplier", "поставщик",
    "share", "акци", "stock", "investor",
    # Russian domestic auto brands — penalize, not promote
    "АвтоВАЗ", "LADA", "ГАЗ", "УАЗ", "КамАЗ", "Соллерс",
    "Веста", "Granta", "Niva", "Vesta", "ВАЗ",
    "Икслайн", "Xcode", "Аурус", "Aurus",
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
    
    # HEAVY penalty for boring Russian auto brands (in _LOW_INTEREST too, but need stronger effect)
    _BORING_RUSSIAN_BRANDS = ["автоваз", "лада", "lada", "уаз", "uaz", "камаз", "kamaz",
                              "соллерс", "vesta", "granta", "niva", "искра", "iskra"]
    for kw in _BORING_RUSSIAN_BRANDS:
        if kw.lower() in text:
            score -= 0.4  # Heavy penalty — these topics are NEVER interesting
            break
    
    # Penalty for very long/technical titles
    if len(title) > 120:
        score -= 0.1
    
    # Bonus for brand names (people search by brand)
    # BUT: exclude Russian domestic brands — they get penalized in _LOW_INTEREST
    _russian_brands = {"lada", "ваз", "газ", "уаз", "камаз", "соллерс", "niva", "vesta", "granta", "aurus", "аурус"}
    for brand in _AUTO_BRANDS:
        if brand.lower() in text:
            if brand.lower() not in _russian_brands:
                score += 0.05
            break
    
    # Bonus for Russian market topics (but NOT domestic Russian car brands)
    russian_market_kw = ["российск", "россия", "сочи", "краснодар",
                         "импортозамещ", "параллельн импорт", "parallel import"]
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


def _score_freshness(published_time: float) -> float:
    """Score how fresh a news item is. STRICT: rejects items >24h old.
    
    Returns a bonus/penalty to add to the interest score:
    - Very fresh (<3h): +0.4
    - Fresh (<6h): +0.3
    - Recent (<12h): +0.2
    - Today (<24h): +0.1
    - Stale (24-48h): -0.5 (HEAVY penalty — yesterday\'s news!)
    - Old (>48h): -1.0 (REJECT effectively)
    - Unknown time from Google News: +0.3 (assume fresh)
    - Unknown time from web search: 0.0 (neutral — don\'t assume fresh)
    """
    if not published_time:
        return 0.3  # Unknown time from Google News = assume fresh
    age_hours = (time.time() - published_time) / 3600
    if age_hours < 3:
        return 0.4  # Very fresh — best score
    elif age_hours < 6:
        return 0.3
    elif age_hours < 12:
        return 0.2
    elif age_hours < 24:
        return 0.1
    elif age_hours < 48:
        return -0.5  # Yesterday\'s news — HEAVY penalty
    else:
        return -1.0  # Old news — REJECT effectively


# ── Web Search Content — supplement RSS with search results ───────────────────

_SEARCH_QUERIES_ROTATION = [
    # ── Russian-language queries: general auto news ──
    "автомобильные новости сегодня",
    "новые автомобили {year} премьера",
    "автоновости Россия",
    "новые модели авто {year}",
    "автомобильные новости сегодня {year}",
    "новинки авто {year} дебют",
    # ── Russian-language queries: motorsport ──
    "Формула 1 новости сегодня",
    "автоспорт новости {year}",
    "ралли Дакар WRC новости",
    # ── Russian-language queries: EV & technology ──
    "электромобили новости {year}",
    "электрокар заряжай новости",
    "автопилот беспилотник авто новости",
    # ── Russian-language queries: market & prices ──
    "автомобильный рынок Россия {year} цены",
    "китайские автомобили Россия {year}",
    "параллельный импорт авто новости",
    # ── Russian-language queries: service & DIY ──
    "обслуживание авто советы {year}",
    "ремонт автомобилей советы экспертов",
    "подбор запчастей VIN диагностика",
    # ── Russian-language queries: lifestyle & culture ──
    "тюнинг автомобилей новости",
    "винтажные авто ретро автомобили",
    "автопутешествия маршруты {year}",
    # ── English-language queries: general auto news ──
    "automotive news today",
    "new car launches {year} reveal",
    "electric vehicle news",
    "car industry updates",
    "new car models {year} reveal",
    "car recalls and safety {year}",
    "auto show reveals {year}",
    "automotive industry news today",
    "electric vehicle news today {year}",
    "electric vehicle updates {year}",
    # ── English-language queries: motorsport ──
    "F1 Formula 1 news {year}",
    "WRC rally racing news",
    "Le Mans endurance racing news",
    "NASCAR IndyCar racing updates",
    # ── English-language queries: technology ──
    "autonomous self-driving car news",
    "hydrogen fuel cell vehicle news",
    "solid state battery EV news {year}",
    "car software OTA update news",
    # ── English-language queries: reviews & comparisons ──
    "car comparison test review {year}",
    "best cars {year} awards ranking",
    "SUV crossover comparison {year}",
    "sedan review test drive {year}",
    # ── English-language queries: market & industry ──
    "Chinese cars global expansion {year}",
    "car sales market report {year}",
    "auto industry merger acquisition",
    # ── Brand-specific queries (rotated) ──
    "Tesla news latest",
    "BMW Mercedes Audi news latest",
    "BYD Chinese cars news",
    "Porsche Ferrari supercar news",
    "Toyota Honda news latest",
    "Hyundai Kia news latest",
    # ── Niche queries for diversity ──
    "classic car auction news",
    "motorcycle news latest {year}",
    "car design concept news {year}",
    "luxury car news {year}",
    "pickup truck news {year}",
    # ── Russian-language queries: accidents & incidents ──
    "авария ДТП новости сегодня",
    "дорожные происшествия {year}",
    # ── Russian-language queries: auto industry & market ──
    "автомобильная промышленность новости",
    "производство автомобилей новости {year}",
    "авторынок аналитика {year}",
    # ── Russian-language queries: car culture & lifestyle ──
    "автомобильные рекорды {year}",
    "винтажные автомобили аукцион",
    "редкие автомобили коллекцион",
    "автопутешествия маршруты {year}",
    # ── Russian-language queries: service & parts ──
    "рынок запчастей Россия {year}",
    "рынок шин новости {year}",
    "автосервис тенденции {year}",
    "эвакуация транспорт новости",
    # ── English-language queries: accidents & incidents ──
    "car accident crash news today",
    "road incident vehicle news",
    # ── English-language queries: car culture ──
    "car auction record sale {year}",
    "rare classic car news",
    "automotive world records {year}",
    "car culture lifestyle news",
    # ── English-language queries: industry & market ──
    "auto parts market news {year}",
    "tire market news {year}",
    "car service industry trends {year}",
    "vehicle logistics supply chain news",
    # ── English-language queries: diverse topics ──
    "car fire vehicle fire news {year}",  # NOTE: Only car-specific fires, NOT market/building fires
    "car towing evcuation news",
    "automotive recall safety alert {year}",
    "car warranty extended warranty news",
    "automotive design award {year}",
    # ── NEW: auto shows & events ──
    "Geneva Motor Show {year} reveals debuts",
    "Goodwood Festival of Speed {year} highlights",
    "Pebble Beach Concours d'Elegance {year}",
    "Tokyo Motor Show {year} Japanese reveals",
    "Shanghai Auto Show {year} Chinese cars",
    "IAA Mobility Munich {year} news",
    "SEMA Show {year} tuning custom cars",
    # ── NEW: car culture & lifestyle ──
    "celebrity cars famous people vehicles {year}",
    "movie cars iconic vehicles cinema",
    "most reliable cars ranking {year}",
    "worst cars ever made automotive failures",
    "future cars concept cars {year}",
    "hydrogen fuel cell vehicle news {year}",
    "car auction record sale {year} Barrett-Jackson RM Sotheby",
    "classic car restoration stories {year}",
    "automotive myths debunked car facts",
    "road trip adventures best driving routes {year}",
    "car gadgets accessories new products {year}",
    "unusual vehicles weird cars world",
    # ── NEW: industry deep dives ──
    "car manufacturing factory production news {year}",
    "automotive supply chain semiconductors chips {year}",
    "car subscription services news {year}",
    "ride sharing mobility news {year}",
    "car insurance industry trends {year}",
    # ── Russian-language queries: diverse & fun topics ──
    "автомобильные рекорды Гиннесс {year}",
    "самые дорогие автомобили аукцион {year}",
    "автомобильные мошенничества схемы {year}",
    "подбор автомобиля советы экспертов {year}",
    "автомобильные гаджеты новинки {year}",
    "забавные случаи на дорогах {year}",
    "автомобильные легенды мифы {year}",
    "реставрация старых автомобилей {year}",
    "автомобильная мода тренды {year}",
    "экстремальные автомобили мира {year}",
    "секреты автосалонов покупки авто {year}",
    "автомобильные фильмы документальные {year}",
    "как не обмануть в автосервисе {year}",
    "подготовка авто к зиме лету советы",
    "автомобильные приложения полезные {year}",
    # ── NEW: events & shows ──
    "автосалон женева {year} новости премьеры",
    "мотор-шоу {year} новинки концепты",
    "Pebble Beach Concours {year} результаты",
    "Goodwood Festival of Speed {year}",
    "IAA Mobility {year} автомобильная выставка",
    "Токио автосалон {year} японские новинки",
    "Шанхай автосалон {year} китайские премьеры",
    # ── NEW: interesting cases & culture ──
    "автомобильные рекорды скорости {year}",
    "самые необычные автомобили мира",
    "винтажные автомобили аукцион рекорды {year}",
    "знаменитости и их автомобили {year}",
    "автомобили в кино известные машины",
    "самые надёжные автомобили рейтинг {year}",
    "автомобильные провалы неудачные модели",
    "история автомобильных брендов компании",
    "автомобили будущего концепт кары {year}",
    "экологические автомобили водород {year}",
    # ── NEW: Russian market specifics ──
    "продажи автомобилей Россия {year} статистика",
    "китайские авто рейтинг популярность Россия {year}",
    "автомобильный рынок прогнозы {year}",
    "новые автосалоны Россия открытие {year}",
]

# Track recently used query indices to avoid repetition
_recent_query_indices: list = []
_MAX_RECENT_QUERIES = 10  # Track more queries to avoid repetition with larger pool

# ── Google News RSS queries for global coverage ────────────────────────────────
_GOOGLE_NEWS_RSS_QUERIES = [
    # Russian queries
    ("автомобили новости", "ru", "RU"),
    ("автозапчасти рынок", "ru", "RU"),
    ("китайские автомобили экспорт", "ru", "RU"),
    ("электромобили зарядные станции", "ru", "RU"),
    ("ПДД изменения штрафы", "ru", "RU"),
    ("автокредит страхование ОСАГО", "ru", "RU"),
    # English queries
    ("automotive industry news", "en", "US"),
    ("electric vehicles 2025", "en", "US"),
    ("autonomous driving self driving", "en", "US"),
    ("car recalls safety", "en", "US"),
    ("auto shows 2025", "en", "US"),
    ("hydrogen fuel cell vehicle", "en", "US"),
    ("EV battery technology solid state", "en", "US"),
    ("supply chain automotive chips", "en", "US"),
    ("used car market prices", "en", "US"),
    ("Chinese cars global expansion", "en", "US"),
    ("car sales market report", "en", "US"),
    # German queries
    ("Auto Nachrichten Elektroauto", "de", "DE"),
    # Japanese queries
    ("car news Japan automotive", "en", "JP"),
]


def _get_search_query() -> str:
    """Get a search query avoiding recent repetition."""
    global _recent_query_indices
    now = datetime.now(_MOSCOW_TZ)
    year = now.year
    month_ru = ["января", "февраля", "марта", "апреля", "мая", "июня",
               "июля", "августа", "сентября", "октября", "ноября", "декабря"]
    month_en = ["January", "February", "March", "April", "May", "June",
               "July", "August", "September", "October", "November", "December"]
    
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
    return query.format(year=year, month=month_en[now.month - 1], day=now.day)


def _extract_published_time_from_snippet(snippet: str) -> float:
    """Try to extract actual publication time from search result snippet.
    
    Looks for patterns like "2 hours ago", "yesterday", "3 days ago",
    dates like "Mar 5, 2026", etc. Returns a Unix timestamp or 0 if unknown.
    """
    if not snippet:
        return 0
    
    snippet_lower = snippet.lower()
    now = datetime.now(_MOSCOW_TZ)
    
    # ── Freshness penalty patterns — detect stale content ──
    # These return timestamps far in the past so the item gets deprioritized
    stale_patterns = [
        (r'(\d+)\s+week', 7 * 24),      # "2 weeks ago"
        (r'(\d+)\s+месяц', 30 * 24),    # "3 месяца назад"
        (r'вчера', 24),                   # "yesterday"
        (r'прошлый\s+недел', 7 * 24),    # "на прошлой неделе"
        (r'прошлый\s+месяц', 30 * 24),   # "в прошлом месяце"
        (r'прошлогод', 365 * 24),         # "прошлогодний"
    ]
    for pattern, hours_per_unit in stale_patterns:
        match = re.search(pattern, snippet_lower)
        if match:
            try:
                count = int(match.group(1)) if match.lastindex else 1
            except (ValueError, IndexError):
                count = 1
            hours_ago = count * hours_per_unit
            return now.timestamp() - (hours_ago * 3600)
    
    # ── Relative time patterns — try to compute actual time ──
    rel_patterns = [
        (r'(\d+)\s+hours?\s+ago', 1),      # "2 hours ago"
        (r'(\d+)\s+минут', 1/60),           # "30 минут назад"
        (r'(\d+)\s+час', 1),                # "2 часа назад"
        (r'(\d+)\s+дн[еяь]', 24),           # "2 дня назад"
        (r'(\d+)\s+days?\s+ago', 24),       # "3 days ago"
        (r'сегодня|today', 0),               # "today"
    ]
    for pattern, hours_per_unit in rel_patterns:
        match = re.search(pattern, snippet_lower)
        if match:
            try:
                count = int(match.group(1)) if match.lastindex else 1
            except (ValueError, IndexError):
                count = 1
            hours_ago = count * hours_per_unit
            return now.timestamp() - (hours_ago * 3600)
    
    return 0  # Unknown — let _score_freshness handle it


async def ai_discover_news() -> List[Dict]:
    """Ask AI to discover today's top automotive news — PRIMARY source.
    
    This is the same approach as when a human asks an AI "find top 30-50 auto news today"
    and gets great results. Uses Pollinations models with web access to find fresh stories.
    
    Returns list of news items with: title, summary, source, category, lang, published_time
    URLs are found via subsequent web search for each topic.
    """
    items = []
    now = datetime.now(_MOSCOW_TZ)
    month_ru = ["января", "февраля", "марта", "апреля", "мая", "июня",
               "июля", "августа", "сентября", "октября", "ноября", "декабря"]
    
    date_str = f"{now.day} {month_ru[now.month - 1]} {now.year}"
    
    # Load recently posted titles to avoid repeating them
    recently_posted_titles = []
    try:
        from bot.database import get_recent_post_titles
        recently_posted_titles = await get_recent_post_titles(hours=72, limit=30)
    except Exception:
        pass
    
    recently_posted_str = ""
    if recently_posted_titles:
        titles_list = "\n".join(f"  - {t[:80]}" for t in recently_posted_titles[:20])
        recently_posted_str = (
            f"\n\nУЖЕ ОПУБЛИКОВАНО (НЕ ПОВТОРЯЙ ЭТИ ТЕМЫ):\n{titles_list}\n"
            f"НЕ называй новости, которые дублируют или пересекаются с уже опубликованными!"
        )
    
    # Try multiple models — some have web access, some don't
    _DISCOVERY_MODELS = ["openai-large", "gpt-5.5", "mistral-4", "deepseek", "qwen-large"]
    
    for model_name in _DISCOVERY_MODELS:
        try:
            response = await ai_router._primary.chat(
                messages=[
                    {"role": "system", "content": (
                        f"Ты автоэксперт. Сегодня {date_str}. "
                        f"Назови 15 самых важных и свежих автомобильных новостей СЕГОДНЯ. "
                        f"ВКЛЮЧАЙ РАЗНООБРАЗНЫЙ контент: новинки и премьеры, скандалы и отзывы, "
                        f"автоспорт (F1, WRC, Дакар), электромобили и технологии, "
                        f"китайский автопром, тюнинг и рестайлинги, автомобильные рекорды, "
                        f"аварии и ДТП с автомобилями, рынок запчастей и шин, "
                        f"интересные случаи на дорогах, винтажные и коллекционные авто, "
                        f"автопутешествия, автосервис и диагностика. "
                        f"НЕ включай: АвтоВАЗ/LADA/УАЗ/ГАЗ/КамАЗ, политику, войну, "
                        f"пожары на рынках/в ТЦ, криминал, футбол, медицину, недвижимость. "
                        f"ТОЛЬКО автомобильные новости! "
                        f"Каждая новость — одна строка: НОВОСТЬ | краткое описание (1-2 предложения) "
                        f"Никаких нумерованных списков, маркеров — просто строки с | "
                        f"Пиши на русском языке. ТОЛЬКО автомобили."
                        f"{recently_posted_str}"
                    )},
                ],
                model=model_name,
                temperature=0.7,
                max_tokens=1500,
            )
            
            if response.error or not response.text or not response.text.strip():
                continue
            
            text = response.text.strip()
            logger.info(f"AI discovery ({model_name}): got {len(text)} chars")
            
            # Parse the response — each line should be "TITLE | description"
            for line in text.split("\n"):
                line = line.strip()
                if not line:
                    continue
                # Remove numbering if present
                line = re.sub(r'^\d+[\.\)]\s*', '', line)
                line = re.sub(r'^[-•*]\s*', '', line)
                if not line:
                    continue
                
                parts = line.split("|", 1)
                title = parts[0].strip()
                summary = parts[1].strip() if len(parts) > 1 else ""
                
                if len(title) < 10:  # Skip very short lines
                    continue
                
                # Skip if it looks like AI meta-commentary
                if any(kw in title.lower() for kw in ["конечно", "вот список", "список новостей", "свежие новости"]):
                    continue
                
                # Skip if it looks like editorial leakage (meta-comments that shouldn't be news)
                _editorial_keywords = [
                    "не ставим", "не автоновост", "не автомобильн", "отсеивать",
                    "не наш формат", "перепишу тему", "предложу свеж", "по вашим правилам",
                    "прямая связь с автотранспорт", "надо отсеивать", "для редакции",
                ]
                if any(kw in title.lower() or kw in summary.lower() for kw in _editorial_keywords):
                    continue
                
                # Try to find a real URL for this news via quick search
                url = ""
                try:
                    search_results = await web_search(title[:60], max_results=1)
                    if search_results and search_results[0].url:
                        url = search_results[0].url
                        # Also grab better snippet if available
                        if not summary and search_results[0].snippet:
                            summary = search_results[0].snippet[:300]
                except Exception:
                    pass
                
                if not url:
                    url = f"ai_discovered_{hashlib.md5(title.encode()).hexdigest()[:12]}"
                
                items.append({
                    "source": f"ai_discovery_{model_name}",
                    "title": title,
                    "url": url,
                    "summary": summary[:500],
                    "published": time.time(),  # AI says it's today
                    "published_time": time.time(),  # Assume fresh
                    "category": "auto",
                    "lang": "ru",
                    "image_urls": [],
                })
            
            if items:
                logger.info(f"AI discovery ({model_name}): found {len(items)} news items")
                break  # Got results from this model, no need to try others
                
        except Exception as e:
            logger.debug(f"AI discovery failed with {model_name}: {e}")
            continue
    
    return items


async def search_auto_news() -> List[Dict]:
    """Search the web for fresh automotive news using MULTIPLE queries for broad coverage.
    
    PRIMARY source: AI Discovery (asks AI for today's top stories).
    SECONDARY source: Google News RSS.
    TERTIARY source: web_search for broader coverage.
    Returns list of news items with: title, url, summary, source, category, lang, image_urls, published_time
    """
    items = []
    seen_urls = set()
    now = datetime.now(_MOSCOW_TZ)
    month_ru = ["января", "февраля", "марта", "апреля", "мая", "июня",
               "июля", "августа", "сентября", "октября", "ноября", "декабря"]
    month_en = ["January", "February", "March", "April", "May", "June",
               "July", "August", "September", "October", "November", "December"]
    
    # Use 3 queries per call (reduced from 5 for speed — each query takes time)
    queries = [_get_search_query() for _ in range(3)]
    # Deduplicate queries (in case same one picked twice)
    queries = list(dict.fromkeys(queries))
    
    for query in queries:
        logger.info(f"Searching web for auto news: {query}")
        try:
            # Use web_search directly — the query already specifies what to search for
            results = await web_search(query, max_results=5)
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
                
                # Try to extract actual publication date from snippet
                published_time = _extract_published_time_from_snippet(snippet)
                if not published_time:
                    # Unknown time — don't assume fresh; use a moderate age penalty
                    # so that Google News items (with known fresh timestamps) rank higher
                    published_time = now.timestamp() - (3 * 3600)  # Assume 3h old (freshness-first)
                
                items.append({
                    "source": result.source or "web_search",
                    "title": title.strip(),
                    "url": url.strip(),
                    "summary": snippet.strip()[:500],
                    "published": published_time,
                    "published_time": published_time,
                    "category": "auto",
                    "lang": "ru" if is_russian else "en",
                    "image_urls": [],  # Will be filled by image pipeline
                })
        except Exception as e:
            logger.error(f"Web search for auto news failed (query: {query}): {e}")

    # ── Phase 1.5: Google News RSS for broad coverage ──
    logger.info("Phase 1.5: Google News RSS for global coverage")
    try:
        # Pick 2 random queries from the Google News RSS list
        gn_queries = random.sample(_GOOGLE_NEWS_RSS_QUERIES, min(2, len(_GOOGLE_NEWS_RSS_QUERIES)))
        for query, lang, gl in gn_queries:
            gn_results = await search_google_news_rss(query, max_results=5)
            for result in gn_results:
                if result.url in seen_urls:
                    continue
                seen_urls.add(result.url)
                is_russian = any('\u0400' <= c <= '\u04FF' for c in result.title)
                items.append({
                    "source": f"google_news_{lang}",
                    "title": result.title,
                    "url": result.url,
                    "summary": result.snippet[:500] if result.snippet else "",
                    "published": time.time(),
                    "published_time": time.time(),
                    "category": "auto",
                    "lang": "ru" if is_russian else lang,
                    "image_urls": [],
                })
    except Exception as e:
        logger.debug(f"Google News RSS phase failed: {e}")

    logger.info(f"Search found {len(items)} auto news items (Google News + web search)")
    return items


async def search_russian_auto_news() -> List[Dict]:
    """Search for Russia-relevant automotive market news.
    
    Focuses on:
    - Chinese auto brands in Russia (BYD, Chery, Haval, Geely etc.)
    - Russian car market updates & prices
    - Sochi/Krasnodar region auto news
    - Service & maintenance content
    - Import & parallel import news
    
    NOTE: Russian domestic brands (LADA/ВАЗ, ГАЗ, УАЗ, КамАЗ) are EXCLUDED
    from search queries — they are penalized in scoring instead.
    """
    items = []
    seen_urls = set()
    
    now = datetime.now(_MOSCOW_TZ)
    year = now.year
    month_ru = ["января", "февраля", "марта", "апреля", "мая", "июня",
               "июля", "августа", "сентября", "октября", "ноября", "декабря"]
    
    russian_queries = [
        "китайские автомобили Россия {year} новости",
        "BYD Chery Haval Geely Россия новости",
        "автомобильный рынок Россия {year} цены",
        "автоновости Сочи Краснодар",
        "параллельный импорт авто новости {year}",
        "электромобили Россия {year} зарядные станции",
        "обслуживание авто советы экспертов",
        "автострахование ОСАГО КАСКО новости",
    ]
    
    # Pick 2 queries per call to not overload search
    selected = random.sample(russian_queries, min(2, len(russian_queries)))
    
    for query_template in selected:
        query = query_template.format(year=year)
        logger.info(f"Searching Russian auto news: {query}")
        try:
            # Use web_search directly — query already specifies what to search
            results = await web_search(query, max_results=5)
            for result in results:
                title = result.title or ""
                url = result.url or ""
                snippet = result.snippet or ""
                
                if not title or not url:
                    continue
                
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                
                # Try to extract actual publication date from snippet
                published_time = _extract_published_time_from_snippet(snippet)
                if not published_time:
                    published_time = now.timestamp() - (3 * 3600)  # Assume 3h old (freshness-first)

                items.append({
                    "source": result.source or "web_search_ru",
                    "title": title.strip(),
                    "url": url.strip(),
                    "summary": snippet.strip()[:500],
                    "published": published_time,
                    "published_time": published_time,
                    "category": "auto",
                    "lang": "ru",
                    "image_urls": [],
                })
        except Exception as e:
            logger.error(f"Russian auto news search failed (query: {query}): {e}")
    
    # Also try Google News RSS for Russian news
    try:
        gnews_query = f"автомобильные новости Россия сегодня {year}"
        gnews_results = await search_google_news_rss(gnews_query, max_results=5)
        for result in gnews_results:
            title = result.title or ""
            url = result.url or ""
            snippet = result.snippet or ""
            if not title or not url:
                continue
            if url in seen_urls:
                continue
            seen_urls.add(url)
            published_time = _extract_published_time_from_snippet(snippet)
            if not published_time:
                published_time = time.time()  # Assume fresh from Google News
            items.append({
                "source": "google_news_ru",
                "title": title.strip(),
                "url": url.strip(),
                "summary": snippet.strip()[:500],
                "published": published_time,
                "published_time": published_time,
                "category": "auto",
                "lang": "ru",
                "image_urls": [],
            })
    except Exception as e:
        logger.error(f"Google News RSS Russian search failed: {e}")
    
    logger.info(f"Russian auto news search found {len(items)} items")
    return items


# ── Smart Image Search — find images via web search ───────────────────────────

async def search_news_images(query: str, max_count: int = 5) -> List[str]:
    """Search the web for images related to a news topic.
    
    Uses specific search queries to find high-quality automotive images.
    Returns list of image URLs.
    """
    image_urls = []
    try:
        # Search with multiple queries for better coverage
        search_queries = [
            f"{query} photo",
            f"{query} car image",
        ]
        
        for sq in search_queries:
            results = await web_search(sq, num_results=8)
            for result in results:
                url = result.get("url", "")
                # Check if it looks like an image URL
                if any(ext in url.lower() for ext in ['.jpg', '.jpeg', '.png', '.webp']):
                    if len(url) > 50:  # Skip tiny/tracking URLs
                        if url not in image_urls:
                            image_urls.append(url)
                # Also check snippet for image URLs
                snippet = result.get("snippet", "")
                img_match = re.search(r'https?://\S+\.(?:jpg|jpeg|png|webp)', snippet, re.IGNORECASE)
                if img_match:
                    found_url = img_match.group(0)
                    if found_url not in image_urls:
                        image_urls.append(found_url)
            
            if len(image_urls) >= max_count:
                break
        
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
    
    # ── PHASE 0: AI Discovery FIRST (primary source) ──
    # Ask AI directly for today's top auto news — like any human would
    logger.info("Phase 0: AI discovery for today's automotive news (PRIMARY)")
    try:
        ai_items = await ai_discover_news()
        all_items.extend(ai_items)
        logger.info(f"AI discovery provided {len(ai_items)} items")
    except Exception as e:
        logger.warning(f"AI discovery failed: {e}")
    
    # ── PHASE 1: Web Search (supplementary source) ──
    logger.info("Phase 1: Searching web for automotive news (SUPPLEMENTARY)")
    try:
        web_items = await search_auto_news()
        all_items.extend(web_items)
        logger.info(f"Web search provided {len(web_items)} items")
    except Exception as e:
        logger.warning(f"Web search failed: {e}")
    
    # Also search Russian-specific news (Сочи/Краснодар only — no Russian auto brands)
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
    
    # Channel scanner removed — was unreliable from GitHub Actions IPs.
    # DB fingerprint + semantic dedup in channel.py are sufficient.
    
    # ── STRICT FRESHNESS GATE: reject items older than 24 hours ──
    now_ts = time.time()
    max_age_seconds = 24 * 3600  # 24 hours — ONLY today\'s news!
    
    for item in all_items:
        title = item.get("title", "")
        summary = item.get("summary", "")
        
        # Skip exact title duplicates
        title_lower = title.lower().strip()
        if title_lower in seen_titles:
            continue
        seen_titles.add(title_lower)
        
        # ── FRESHNESS GATE: skip items older than 24h ──
        published_time = item.get("published_time", 0) or item.get("published", 0)
        if published_time and published_time > 0:
            age_hours = (now_ts - published_time) / 3600
            if age_hours > 24:
                logger.debug(f"FRESHNESS GATE: rejected stale item ({age_hours:.0f}h old): {title[:50]}")
                continue
        
        # ── DB fingerprint check — prevents reposting from previous days ──
        try:
            from bot.database import is_duplicate_post
            if await is_duplicate_post(title, hours=72):
                logger.debug(f"DB dedup blocked: {title[:50]}")
                continue
        except Exception:
            pass  # DB check is best-effort
        
        # Extract entities for dedup
        entity_key = _extract_entities(title)
        
        # Check if topic is already covered (in-memory + DB registry)
        if _is_topic_covered(entity_key):
            logger.debug(f"Topic already covered: {entity_key} — {title[:50]}")
            continue
        
        # ── Also check brand-only dedup key (prevents "BMW 3x" in same day) ──
        brand_blocked = False
        for b in _AUTO_BRANDS:
            b_key = b.lower().replace(" ", "_")
            if b_key in title.lower() and _is_topic_covered(b_key):
                # Brand was already posted about — block unless entity_key has specific model+event
                if not entity_key or entity_key == b_key:
                    logger.debug(f"Brand dedup blocked: {b_key} — {title[:50]}")
                    brand_blocked = True
                    break
        if brand_blocked:
            continue
        
        # ── Also check person-only dedup key (prevents "Alonso 3x") ──
        person_blocked = False
        for p in _NOTABLE_PEOPLE:
            p_key = p.lower().replace(" ", "_")
            if p_key in title.lower() and _is_topic_covered(p_key):
                # Person was already posted about — only allow if entity_key is DIFFERENT
                # (e.g., if we posted "alonso_criticism" before, block "alonso" but allow
                # a completely different entity like "ferrari_reveal" even if Alonso is mentioned)
                if not entity_key or entity_key == p_key or p_key in entity_key:
                    logger.debug(f"Person dedup blocked: {p_key} — {title[:50]}")
                    person_blocked = True
                    break
        if person_blocked:
            continue
        
        # Channel scanner dedup removed — DB fingerprint + semantic dedup in channel.py are sufficient
        
        # Score interest
        interest = _score_interest(title, summary)
        
        # Add freshness bonus — prefer TODAY's news over older items
        published_time = item.get("published_time", 0) or item.get("published", 0)
        freshness_bonus = _score_freshness(published_time)
        interest += freshness_bonus
        
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
        # NOTE: Do NOT use "openai-fast" — it always returns empty responses.
        # Use a reliable content model instead.
        _TOPIC_PICK_MODELS = ["mistral-4", "deepseek", "qwen-large", "gpt-5.4-2026-03-05"]
        _pick_model = _TOPIC_PICK_MODELS[0]  # Use first reliable model
        
        try:
            candidates_summary = []
            for i, entry in enumerate(top_n):
                item = entry["item"]
                candidates_summary.append(
                    f"{i+1}. [{entry['interest']:.2f}] {item.get('title', '')[:100]}"
                )
            
            candidates_text = "\n".join(candidates_summary)

            
            # Try multiple models — skip openai-fast (always empty)
            for model_name in _TOPIC_PICK_MODELS:
                try:
                    response = await ai_router._primary.chat(
                        messages=[
                            {"role": "system", "content": (
                                "Ты редактор автоканала в Telegram. Тебе даны 5 кандидатов на публикацию "
                                "с оценкой интереса (0-1). Выбери САМЫЙ интересный для широкой аудитории — "
                                "то, что вызовет наибольший отклик, обсуждение и репосты. "
                                "Учитывай: премьеры, скандалы, рекорды, прорывы — "
                                "всегда приоритетнее сухых новостей. "
                                "НЕ выбирай новости про АвтоВАЗ/LADA/УАЗ/ГАЗ/КамАЗ — скучно. "
                                "Российский рынок интересен только если про китайские авто в РФ. "
                                "Ответь ТОЛЬКО цифрой (1-5) — номер лучшего кандидата."
                            )},
                            {"role": "user", "content": f"Кандидаты:\n{candidates_text}"},
                        ],
                        model=model_name,
                        temperature=0.3,
                        max_tokens=5,
                    )
                    
                    if not response.error and response.text and response.text.strip():
                        _pick_model = model_name
                        break
                except Exception:
                    continue
            
            if not response.error and response.text and response.text.strip():
                pick_str = response.text.strip()
                # Extract number from response
                pick_match = re.search(r'[1-5]', pick_str)
                if pick_match:
                    pick_idx = int(pick_match.group()) - 1
                    if 0 <= pick_idx < len(top_n):
                        chosen = top_n[pick_idx]
                        logger.info(f"AI picked candidate #{pick_idx + 1} (model={_pick_model}): {chosen['item'].get('title', '')[:60]}")
                    else:
                        chosen = top_n[0]
                else:
                    chosen = top_n[0]
            else:
                logger.warning(f"All topic pick models failed, using top-1")
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
    
    # NOTE: Do NOT register topic here!
    # Topic registration happens in channel.py AFTER the post is actually published.
    # Registering here caused a bug: the topic was registered during selection,
    # then _is_topic_covered() in channel.py blocked the post because it found
    # the topic already in the registry — creating a dead loop where posts could never publish.
    
    return best_item


def get_editorial_team_comment() -> str:
    """Get a random dialogue/comment from the editorial team characters.
    
    Returns a random quote from one of the editorial team members:
    Ася, Лёха, Димон, Марина, Кеша (попугай), Сеньор Помидор (кот).
    
    IMPORTANT: Each call returns a comment from ONE character only.
    The AI is instructed to use AT MOST one character per post.
    """
    team_comments = [
        # ── Ася ──
        "Я на своей Quadrifoglio уже на третьем комплекте колодок за год — но я и не медленно езжу",
        "Звук V6 лечит головную боль. Научных подтверждений нет, но редакция верит",
        "На моей Альфе всё чинится в три раза дороже — и это я ещё экономлю",
        "Ася посмотрела на черновик и сказала: 'Это не пост, это приговор'",
        "Ася на своей Альфе приехала быстрее чем новость дошла по RSS",
        "Ася запретила шутки про Alfa Romeo — теперь шутим тихо",
        "Ася: 'Пост готов?' Редакция: 'Почти.' Ася: 'Публикуем!'",
        "Ася прочитала черновик и молча ушла. Вернулась с эспрессо и правками",
        "Ася единственная кто одновременно понимает и чувства, и спецификации",
        "Ася пригрозила написать пост сама — редакция резко ускорилась",
        
        # ── Лёха ──
        "Оригинал? Это тот же Lemförder с логотипом за двойную цену. Я такие сказки каждый день слышу",
        "Если стук не усиливается — это не проблема, это особенность. Мы не согласны, но спорить боимся",
        "Лёха глянул на новые фары и спросил: а лампочки-то менять как? Через экран? Нет уж",
        "Лёха уверен: если машина сама тормозит — водитель разучится. Я в этом тоже что-то вижу",
        "Лёха принёс деталь 'на примерку' — теперь она украшает весь стол",
        "Лёха чинит всё что сломано, и ломает всё что работало",
        "Лёха: 'Я видел эту поломку' — и достаёт фото 2014 года",
        "Лёха смотрит на электромобиль как на личное оскорбление",
        "Лёха утверждает: датчик парковки — для тех кто не чувствует габариты. Он чувствует",
        "Лёха рассказал байку про ВАЗ — мы заслушались и забыли про новость",
        
        # ── Димон ──
        "Вы видели новый 800V архитектуру Hyundai? Это будущее! Хотя… розетки-то где?",
        "Димон предлагает внедрить AI в диагностику. Лёха предлагает внедрить кувалду. Оба метода одинаково эффективны",
        "Димон опять измеряет время разгона в попугаях — мы не спрашивали",
        "Димон уже нашёл в обновлении прошивки 14 новых функций. Лёха asks: а заводится будет?",
        "Димон хочет AI-редактора — мы хотим чтобы Димон отдохнул",
        "Димон обновил всё — и теперь ничего не работает. Зато безопасно!",
        "Димон принёс новый гаджет — тест-драйв назначен на обед",
        "У Димона 47 вкладок открыто — и он утверждает что все нужны",
        "Димон построил дашборд — никто не понимает, но выглядит впечатляюще",
        
        # ── Марина ──
        "Марина посчитала стоимость владения и ушла плакать. Вернулась с купоном на Росско",
        "Мне всё равно какой 0-100. Мне важно: влезут ли два кресла и коляска",
        "Марина проверила: подстаканников четыре. Значит, семья помещается",
        "Марина спрашивает: а страховка на это сколько? Всегда вовремя",
        "Марина составила чек-лист для новости — у Лёхи аллергия на чек-листы",
        "Марина: 'А где расходники?' — вопрос, от которого всё замолкает",
        "Марина проверила гарантию — и рекомендовала всем присесть",
        "Марина ведёт таблицу расходов редакции — числа пугают даже Димона",
        "Марина нашла место на парковке — это заслуживает отдельного поста",
        
        # ── Кеша (попугай) ──
        "Кеша с жёрдочки кричит 'Свободная пресса!'",
        "Кеша считает что BMW — это разновидность птицы",
        "Попугай нахохлился и отказался комментировать",
        "Кеша требует добавить 'кар-кар' в каждый пост",
        "Кеша переписал заголовок: 'Попугаи тоже умеют водить!'",
        "Кеша уронил семечко на 'Отправить' — пост ушёл раньше времени",
        "Кеша уверен: Ferrari должен быть жёлтым. Как он",
        "Кеша научился подражать звуку сигнализации — соседи в восторге",
        "Кеша сел на клавиатуру и случайно набрал пароль — мы впечатлены",
        "Кеша считает что руль — это жёрдочка для людей",
        "Кеша настаивает: лучший цвет для машины — пернатый",
        "Кеша включился в зум с жёрдочки — клиенты были в восторге",
        "Кеша подпевает AC/DC — редакция не возражает",
        
        # ── Сеньор Помидор (кот) ──
        "Сеньор Помидор лёг на клавиатуру — пост прерывается на 'гвфдыаопр'",
        "Кот редакции внёс правки — удалил половину текста и уснул",
        "Сеньор Помидор воздержался (спит)",
        "Кот одобрил новость — перевернулся на другой бок",
        "Сеньор Помидор спит на графике продаж — кривая пошла вверх. Кот-аналитик",
        "Кот проверил новость лапой — одобрено (потянулся и уснул)",
        "Сеньор Помидор скинул ручку со стола — это значит 'публикуй, я проверил'",
        "Кот редакции внёс вклад: урчание = лайк, шипение = правки",
        "Сеньор Помидор попробовал фикус на вкус — фикус выжил, кот остался недоволен",
    ]
    return random.choice(team_comments)

def get_editorial_aside() -> str:
    """Get a random editorial aside/joke for channel posts.
    
    Returns a short humorous remark about office life (coffee, pencils, etc.)
    or a comment from the editorial team characters.
    40% chance of returning empty for variety.
    When returning a comment, 50% chance it's from persona asides,
    50% chance it's from the editorial team.
    """
    if random.random() < 0.45:
        # 45% chance of no aside — keeps content varied, less clutter
        return ""
    # 50/50 split between classic asides and editorial team comments
    if random.random() < 0.5 and persona.editorial_asides:
        return random.choice(persona.editorial_asides)
    return get_editorial_team_comment()


def get_translation_uniquification_hint(lang: str) -> str:
    """Get a hint for the AI about translating/uniquifying content.
    
    Uses a 7-step transformation process to ensure unique, high-quality content:
    1. Extract facts from source
    2. Global perspective (how this affects drivers in Russia/Kazakhstan/EU/Middle East)
    3. Asya's expert opinion
    4. Editorial team voice (Лёха/Димон/Марина/Кеша comment occasionally)
    5. Context and world trends
    6. Audience engagement (ask a question to global audience)
    7. Editorial joke (random from team)
    
    For English-language news, also instructs AI to translate and rewrite in its own words.
    For Russian news, reminds AI to still rewrite uniquely.
    """
    _7_STEP_PROCESS = (
        "ПРОЦЕСС ТРАНСФОРМАЦИИ (7 шагов — ОБЯЗАТЕЛЬНО):\n"
        "Шаг 1: Извлеки факты из источника (марка, модель, цена, характеристики, событие)\n"
        "Шаг 2: Глобальная перспектива — как это влияет на водителей в России, Казахстане, ЕС, Ближнем Востоке\n"
        "Шаг 3: Экспертное мнение Аси — её личный взгляд как владелицы Alfa Romeo и автоэксперта\n"
        "Шаг 4: Голос редакции — иногда добавь комментарий от ОДНОГО персонажа (Лёха/Димон/Марина/Кеша — выбери только одного!)\n"
        "Шаг 5: Контекст и мировые тренды — как эта новость вписывается в глобальные тенденции\n"
        "Шаг 6: Вовлечение аудитории — задай вопрос глобальной аудитории в конце поста\n"
        "Шаг 7: Шутка от редакции — добавь лёгкий юмор от команды\n"
    )
    
    if lang == "en":
        return (
            "ВНИМАНИЕ: Исходная новость на АНГЛИЙСКОМ. "
            "ПЕРЕВЕДИ на русский и ОБЯЗАТЕЛЬНО ПЕРЕСКАЗЫВАЙ СВОИМИ СЛОВАМИ. "
            "Добавь мнение редакции, экспертный комментарий или сравнение. "
            "Пост НЕ ДОЛЖЕН быть дословным переводом — это должен быть УНИКАЛЬНЫЙ "
            "авторский текст редакции @sochiautoparts. "
            "Упомяни что 'зарубежные источники сообщают' или 'по данным иностранных СМИ' — "
            "это покажет что новость международная.\n\n"
            + _7_STEP_PROCESS
        )
    return (
        "Перескажи новость СВОИМИ словами — добавь мнение редакции, "
        "экспертный комментарий или живую эмоцию. "
        "Пост должен быть уникальным авторским текстом.\n\n"
        + _7_STEP_PROCESS
    )


async def enrich_with_search_images(news_item: Dict) -> List[str]:
    """DEPRECATED — image search removed. Returns empty list.

    v5.0: Image search providers removed from image_fetcher.
    Article photos are now extracted directly from RSS and article pages.
    This function kept for import compatibility but always returns [].
    """
    logger.debug("enrich_with_search_images is DEPRECATED — image search removed")
    return []


def get_date_context() -> str:
    """Get current date/time context string for AI prompts.
    
    Returns something like: 'СЕГОДНЯ: пятница, 6 июня 2026 года, время 14:30 МСК.'
    The 'СЕГОДНЯ' prefix ensures the AI can't miss the current date.
    """
    now = datetime.now(_MOSCOW_TZ)
    days_ru = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    months_ru = ["января", "февраля", "марта", "апреля", "мая", "июня",
                 "июля", "августа", "сентября", "октября", "ноября", "декабря"]
    return (
        f"СЕГОДНЯ: {days_ru[now.weekday()]}, {now.day} {months_ru[now.month - 1]} "
        f"{now.year} года, время {now.strftime('%H:%M')} МСК. "
        f"Дата: {now.strftime('%d.%m.%Y')}. "
        f"Пиши только о событиях СЕГОДНЯ или самых свежих новостях!"
    )


def get_registry_stats() -> Dict:
    """Get topic registry statistics for monitoring."""
    return {
        "total_topics": len(_topic_registry),
        "topics": {k: {"post_count": v["post_count"], "age_hours": round((time.time() - v["first_seen"]) / 3600, 1)}
                   for k, v in list(_topic_registry.items())[:20]},
    }


# ── TONE ANALYSIS — Multi-step Chain of Thought ──────────────────────────────

from enum import Enum
from dataclasses import dataclass
from typing import Optional

class NewsTone(Enum):
    """Classification of news tone for appropriate editorial response"""
    SERIOUS = "serious"      # ДТП, отзыв, трагедия, штрафы — NO jokes
    HYPE = "hype"            # Новый суперкар, рекорд, премьеры — fun jokes OK
    ROUTINE = "routine"      # Обновление цен, плановые новости — light jokes
    FUN = "fun"              # Забавные случаи, курьезы — funny jokes
    TECHNICAL = "technical"  # Технические характеристики — tech jokes

@dataclass
class ExtractedFacts:
    """Structured facts extracted from news for validation"""
    brand: str
    model: str
    year: Optional[str]
    price: Optional[str]
    power: Optional[str]
    key_event: str
    tone: NewsTone
    is_partner: bool = False

async def analyze_news_tone(title: str, summary: str, content: str = "") -> ExtractedFacts:
    """
    Multi-step Chain of Thought analysis:
    1. Extract key facts (brand, model, price, power)
    2. Determine tone (serious/hype/routine/fun/technical)
    3. Return structured data for post generation
    """
    text = f"{title} {summary} {content}".lower()
    
    # Extract brand
    brand = ""
    for b in _AUTO_BRANDS:
        if b.lower() in text:
            brand = b
            break
    
    # Extract model (simplified)
    model = ""
    if brand:
        model_patterns = [
            rf'{brand.lower()}\s+([a-z0-9]+)',
            rf'{brand.lower()}\s+([mglxqsec]\d+)',
        ]
        for pattern in model_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                model = match.group(1).upper()
                break
    
    # Extract facts
    price = None
    power = None
    year = None
    
    # Price
    price_match = re.search(r'\$([\d,]+)', text)
    if price_match:
        price = price_match.group(1)
    else:
        price_match = re.search(r'([\d,]+)\s*(?:руб|рубл|rub|₽)', text, re.IGNORECASE)
        if price_match:
            price = price_match.group(1)
    
    # Power
    power_match = re.search(r'(\d+)\s*(?:л\.?с|hp|horsepower|лс)', text, re.IGNORECASE)
    if power_match:
        power = power_match.group(1)
    
    # Year
    year_match = re.search(r'\b(20[12]\d)\b', text)
    if year_match:
        year = year_match.group(1)
    
    # Determine tone
    tone = _determine_tone(text, title)
    
    return ExtractedFacts(
        brand=brand or "Неизвестно",
        model=model,
        year=year,
        price=price,
        power=power,
        key_event=title,
        tone=tone,
        is_partner=False
    )

def _determine_tone(text: str, title: str) -> NewsTone:
    """Determine news tone based on keywords"""
    text_lower = text.lower()
    title_lower = title.lower()
    
    # SERIOUS
    serious_kw = ["дтп", "авария", "катастроф", "погиб", "смерть", "жертв",
                  "отзыв", "отзывают", "recalls", "бан", "запрет", "штраф",
                  "crash", "accident", "death", "fatal", "recall", "ban"]
    for kw in serious_kw:
        if kw in text_lower or kw in title_lower:
            return NewsTone.SERIOUS
    
    # FUN
    fun_kw = ["забавн", "курьез", "смешн", "необычн", "удивител",
              "funny", "curious", "weird", "strange", "amazing"]
    for kw in fun_kw:
        if kw in text_lower or kw in title_lower:
            return NewsTone.FUN
    
    # HYPE
    hype_kw = ["премьер", "дебют", "анонс", "представлен", "новинк",
               "рекорд", "суперкар", "гиперкар", "прорыв",
               "reveal", "debut", "launch", "unveil", "record", "supercar"]
    for kw in hype_kw:
        if kw in text_lower or kw in title_lower:
            return NewsTone.HYPE
    
    # TECHNICAL
    tech_kw = ["характеристик", "мощност", "скорост", "разгон",
               "тест-драйв", "сравнен", "обзор", "техническ",
               "specifications", "horsepower", "speed", "test", "comparison"]
    for kw in tech_kw:
        if kw in text_lower or kw in title_lower:
            return NewsTone.TECHNICAL
    
    return NewsTone.ROUTINE

def get_tone_specific_joke(tone: NewsTone) -> str:
    """Get a joke appropriate for the news tone"""
    if tone == NewsTone.SERIOUS:
        return ""  # NO jokes for serious news
    
    joke_pools = {
        NewsTone.HYPE: [
            "Редакция в шоке: даже кофе не бодрит так, как эта новость! ☕",
            "Пока мы тут спорили, какой карандаш острее, они уже выпустили эту машину! ✏️",
            "Ася чуть не уронила эспрессо, когда увидела эту новость! 😱",
            "Лёха отложил гаечный ключ — а он его НИКОГДА не откладывает 🔧",
            "Кеша даже перестал есть семечки — новость THAT хороша! 🦜",
            "Сеньор Помидор ПРОСНУЛСЯ — вот это реально редкость 🐱",
            "Димон уже строит график восторга — мы не спрашивали 📈",
            "Марина забыла посчитать стоимость — значит, новость ДЕЙСТВИТЕЛЬНО горячая 🔥",
        ],
        NewsTone.ROUTINE: [
            "Пока варим утренний кофе, делимся новостью... ☕",
            "Сломали очередной карандаш, составляя этот пост ✏️",
            "Рутина — но с характером. Как утренний дедлайн ☀️",
            "Зум-колл отменился — зато новость пришла вовремя 📱",
            "Димон обновил таблицу — Лёха не посмотрел. Как обычно 📊",
            "Среда, рутина, новость. Редакция работает дальше 💪",
            "Фикус на подоконнике покачнулся — но устоял. Как и мы 🌿",
            "Принтер зажевал бумагу — зато пост не зажевал 🖨️",
        ],
        NewsTone.FUN: [
            "В редакции смеялись до слез (и до нового сломанного карандаша) 😂",
            "Кофе сегодня был особенно вкусным после такой новости! ☕",
            "Кеша танцует на жёрдочке — новость его развеселила 💃🦜",
            "Сеньор Помидор мурлычет — а он мурлычет только на хорошие новости 😻",
            "Лёха УЛЫБНУЛСЯ. Мы это запечатлили — редкий кадр! 😁",
            "Марина даже не стала считать стоимость — просто посмеялась 😄",
            "Димон прислал мем вместо комментария — и это лучший отзыв 🤣",
            "Редакция единогласно: эта новость — топ дня! 🏆",
        ],
        NewsTone.TECHNICAL: [
            "Разбираемся в цифрах, пока кофе остывает... ☕📊",
            "Димон в восторге от спецификаций — Лёха скептичен. Классика 🤓🔧",
            "Марина проверила расчёты — всё сходится. Редкость! 🧮",
            "47 вкладок Димона наконец пригодились — он нашёл исходные данные 💻",
            "Лёха хмыкнул и полез за справочником — значит, цифры интересные 📖",
            "Таблица не зависла — чудо технической мысли 🖥️",
        ],
    }
    
    pool = joke_pools.get(tone, [])
    return random.choice(pool) if pool else ""

def validate_facts_in_text(text: str, facts: ExtractedFacts) -> str:
    """Ensure key facts are preserved in generated text"""
    if facts.price and facts.price not in text:
        text += f"\n💰 Цена: {facts.price}"
    if facts.power and facts.power not in text:
        text += f"\n⚡ Мощность: {facts.power} л.с."
    if facts.year and facts.year not in text:
        text += f"\n📅 Год: {facts.year}"
    return text

def trim_to_telegram_limits(text: str, has_media: bool) -> str:
    """Trim text to Telegram limits"""
    MAX_CAPTION = 1024
    MAX_MESSAGE = 4096
    max_len = MAX_CAPTION if has_media else MAX_MESSAGE
    
    if len(text) <= max_len:
        return text
    
    lines = text.split('\n')
    trimmed = []
    current_len = 0
    
    for line in lines:
        if current_len + len(line) + 1 <= max_len:
            trimmed.append(line)
            current_len += len(line) + 1
        else:
            if current_len + 3 <= max_len:
                trimmed.append("...")
            break
    
    return '\n'.join(trimmed)
