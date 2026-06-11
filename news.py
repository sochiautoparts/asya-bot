"""
News Engine — AUTO-FOCUSED RSS fetching, international news + translation.
Fetches from multiple sources, deduplicates, stores in DB.
Extracts real images from RSS feeds and article pages for channel posts.
"""

import feedparser
import httpx
import time
import logging
import re
from typing import List, Dict, Optional
from datetime import datetime

from bot.config import config, news_config, NewsSource
from bot.database import add_news_item, get_unposted_news, mark_news_posted, is_duplicate_post

logger = logging.getLogger("asya.news")

# ── Fingerprint-based deduplication ────────────────────────────────────────────
# Stores simplified title fingerprints to prevent similar news from being added
_recent_fingerprints: set = set()


def _compute_fingerprint(title: str) -> str:
    """Compute a simplified fingerprint for dedup.
    Remove articles, lowercase, take first 5 words.
    """
    import re
    # Remove common articles/prepositions
    cleaned = re.sub(r'\b(в|на|с|о|у|по|из|за|от|до|к|не|и|но|а|что|как|это|тот|этот|для|при|через|между|после|перед|без|под|над|об|со|из-за|то|же|ли|бы|уже|ещё|еще|также|тоже|или|либо)\b', '', title.lower())
    cleaned = re.sub(r'[^a-zа-яё0-9]', ' ', cleaned)
    words = cleaned.split()
    return ' '.join(words[:5])


def _fingerprint_matches_existing(fingerprint: str) -> bool:
    """Check if a fingerprint matches any recently used fingerprint (edit distance on first 4 words)."""
    fp_words = fingerprint.split()[:4]
    for existing in _recent_fingerprints:
        ex_words = existing.split()[:4]
        # Count matching words
        matches = sum(1 for w in fp_words if w in ex_words)
        if matches >= 3:
            return True
    return False


def reset_fingerprints():
    """Reset fingerprint set.
    
    NOTE: Do NOT call this at the start of each fetch cycle!
    Previously, resetting fingerprints every cycle caused the same news to be
    re-added across cycles. Now fingerprints persist within the process lifetime.
    Only call this if you explicitly want to clear the in-memory cache.
    """
    global _recent_fingerprints
    _recent_fingerprints = set()

# ── Keywords for auto-relevance filtering ──────────────────────────────────────

AUTO_KEYWORDS_RU = [
    "авто", "автомобиль", "машина", "мотор", "двигатель", "кузов", "салон",
    "шоссе", "дорог", "скорост", "транспорт", "запчас", "ремонт", "сервис",
    "шин", "колес", "топлив", "бензин", "дизел", "электромобиль", "гибрид",
    "тест-драйв", "обзор", "новинка", "модель", "бренд", "марка",
    "продаж", "авторынок", "рынок авто", "рынок запчас", "стоимост", "цен", "акциз", "таможен",
    "автошоу", "концепт", "прототип", "рестайлинг", "поколен",
    "коробка", "коробк", "привод", "подвес", "тормоз", "рулев",
    "пробег", "пробле", "поломк", "диагност", "VIN", "ОСАГО", "КАСКО",
    "автосалон", "дилер", "кредит", "лизинг", "страхов",
    "гонк", "ралли", "формул", "F1", "WRC", "Дакар",
    "LADA", "ВАЗ", "KIA", "Hyundai", "Toyota", "BMW", "Mercedes", "Volkswagen",
    "Audi", "Renault", "Skoda", "Nissan", "Mazda", "Honda", "Ford",
    "Porsche", "Lexus", "Volvo", "Subaru", "Suzuki", "Mitsubishi",
    "Chery", "Haval", "Geely", "Changan", "Exeed", "Tank",
    "Tesla", "BYD", "Zeekr", "Li Auto", "NIO",
    # ── Расширенные автомобильные ключевые слова ──
    "эвакуац", "дтп", "авари", "дорожн", "перекрыт", "затор", "пробк",
    "автозаправ", "азс ", "заправк", "бензоколонк",
    "шиномонтаж", "автомойк", "автохим",
    "перебои поставк", "дефицит запчас",
    "логистик", "грузоперевозк", "автоперевозк",
    "растаможк", "таможен оформл", "параллельн импорт",
    "эвакуатор", "техпомощ", "техосмотр",
    "автокредит", "автострахов", "каско", "осаго",
]

AUTO_KEYWORDS_EN = [
    "car", "auto", "automobile", "vehicle", "motor", "engine", "drive",
    "road", "speed", "transport", "spare", "repair", "service",
    "tire", "wheel", "fuel", "gasoline", "diesel", "electric", "hybrid",
    "test drive", "review", "new model", "brand", "launch",
    "sale", "market", "price", "cost",
    "auto show", "concept", "prototype", "redesign", "generation",
    "transmission", "suspension", "brake", "steering",
    "racing", "rally", "formula", "F1", "WRC", "dakar",
    "EV", "BEV", "PHEV", "ICE", "autonomous", "self-driving",
    "sedan", "SUV", "crossover", "hatchback", "coupe", "convertible",
    "horsepower", "torque", "MPG", "range",
    # ── Extended automotive keywords ──
    "evacuation", "tow truck", "roadside assistance",
    "gas station", "fuel station", "refueling",
    "tire service", "car wash", "auto chemical",
    "supply chain", "parts shortage", "logistics",
    "customs clearance", "parallel import",
    "car loan", "auto insurance",
    "traffic", "road accident", "collision",
    "road closure", "congestion", "jam",
    "car fire", "vehicle fire", "car burned",
    "auto transport", "car carrier", "vehicle shipping",
]

AUTO_KEYWORDS_DE = [
    "Auto", "Automobil", "Fahrzeug", "Motor", "Antrieb", "Straße",
    "Reparatur", "Service", "Reifen", "Benzin", "Diesel", "Elektro",
    "Neuvorstellung", "Modell", "Marke", "Verkauf", "Preis",
    "Getriebe", "Federung", "Bremse", "Lenkung",
    "Renn", "Formel", "SUV", "Limousine",
]

# ── Blocklist: political/war keywords — news with these are REJECTED ───────────

BLOCK_KEYWORDS_RU = [
    # Политика
    "путин", "кремль", "госдума", "едрос", "единая россия", "кпрф",
    "навальн", "оппозиц", "протест", "митинг", "закон о", "законопроект",
    "санкци", "эмбарго", "министр", "правительств", "президент",
    "депутат", "сенатор", "губернатор", "мэр ", "выборы ", "голосован",
    "политик", "парламент", "конституц", "референдум", "выборы президента", "выборы в парламент",
    # Война / СВО
    "сво ", "специальная военная", "вооруженн", "военные действ",
    "мобилизац", "призывник", "окоп", "обстрел", "ракетн удар",
    "террор", "б ое",
    "украин", "крымск", "донбас", "луганск", "донецк", "херсонск", "запорожск",
    "белорусс", "нато", "nato",
    # Общие блокировки
    "ковид", "коронавирус", "пандем", "вакцин",
    # Российские автобренды — не интересны аудитории
    "автоваз", "lada веста", "lada granta", "lada niva", "уаз патриот",
    "камаз новости", "газель новости", "соллерс новости",
    # ── РАСШИРЕННЫЙ БЛОК: неавтомобильные темы ──
    # Автомобильная новость = ПРЯМАЯ связь с автотранспортом:
    #   сгоревшие машины, перебои запчастей, рынок шин, логистика запчастей,
    #   автосервис, эвакуация, ДТП с участием транспорта, дорожные происшествия
    # ВСЁ ОСТАЛЬНОЕ — БЛОКИРУЕТСЯ:
    "пожар на рынке", "пожар на базаре", "пожар в тц", "пожар в магазине",
    "пожар в торгов", "пожар на складе", "загорелся рынок",
    "возгорание на рынке", "возгорание в тц", "возгорание в торгов",
    "пожар в торговом", "пожар в бизнес", "пожар в офис",
    "пожар уничтожил", "пожар повредил", "пожар охватил",
    "горящий рынок", "горящий тц", "горящий торговый",
    "торговый центр пожар", "торговый центр загорел",
    "рынок загорел", "рынок сгорел", "базар сгорел",
    "убийств", "преступлен", "криминал", "грабёж", "грабеж", "разбой",
    "насилие", "домашнее насили", "изнасилован", "педофил",
    "кража", "воровств", "мошенничеств", "коррупци",
    "наркотик", "наркобизнес", "драгдилер",
    "пожар в квартире", "пожар в доме", "пожар в здании",
    "пожар в жилом", "пожар в многоквартирн",
    "обрушение здания", "обрушение крыши", "прорыв трубы",
    "наводнен", "затоплен", "подтоплен", "паводок",
    "землетрясен", "цунами", "ураган", "торнадо",
    "температура воздуха", "погода в", "прогноз погоды",
    "медицин", "больниц", "врач", "лечени", "операция пациент",
    "образован", "школьник", "учитель", "экзамен", "егэ", "огэ",
    "спорт ", "футбол", "хоккей", "олимпи", "чемпионат мир",
    "культур", "театр", "кино ", "фильм", "сериал",
    "ресторан", "кафе ", "рецепт ", "готовить",
    "недвижимост", "квартир цена", "ипотек",
    "криптовалю", "биткоин", "ethereum", "биржа ",
    "квартирный вопрос", "жкх", "коммуналк",
    "пожар на промышл", "взрыв на заводе", "взрыв газа",
    "авиакатастроф", "крушение самолёт", "крушение самолет",
    "железнодорожн", "поезд ", "метро ",
    "утонул", "купаться", "пляж ", "море ",
    "пожар в авто", "сгорел гараж",
    "пожар на вещевом рынке", "горит рынок", "возгорание на рынке",
    "рынок пылает", "торговые ряды пожар", "пожар на оптовом",
    "пожар на вещевой базе", "рынок загорелся от",
    "не автомобильная новость", "не автоновость",
    "тему в канал не ставим", "в канал не ставим",
    # Editorial/meta-commentary leakage — AI should never generate these as news
    "отсеивать", "отсеять", "перепишу тему", "предложу свеж",
    "не наш формат", "по вашим правилам", "прямая связь с автотранспорт",
    "автоформат", "для редакции", "не для публикации",
    # Дополнительные неавтомобильные темы
    "пожар в павильон", "пожар в ларьк", "пожар в киоск",
    "рынок пожар", "базар пожар", "торговый пожар",
    "сгорел рынок", "сгорел магазин", "сгорел склад",
    "торговый центр загор", "молл загорел", "молл пожар",
    "шопинг ", "распродаж", "скидк", "акция магаз",
    "звезд", "шоу-биз", "скандал звезд", "инстаграм",
    "свадьб", "развод звезд", "беременност",
    "диет", "фитнес", "похуден",
    "курорт", "отдых", "туризм ", "виз ",
    "банкрот", "компания обанкрот", "компания закрыл",
    "увольнен", "сокращен", "забастовк",
    "эпидеми", "вирус", "заражен",
]

BLOCK_KEYWORDS_EN = [
    # ── Political/war — strict block ──
    "putin", "kremlin", "war in ukraine", "russia-ukraine", "invasion",
    "sanction", "embargo", "mobiliz", "military", "troop", "missile strike",
    "nato expansion", "conflict zone", "battlefield", "casualt",
    "navalny", "opposition leader",
    "covid", "coronavirus", "pandemic", "vaccin",
    # Boring Russian domestic auto brands
    "lada", "avtovaz", "uaz", "kamaz", "soldis",
    "vesta", "granta", "niva", "iskra",
    # ── NON-AUTOMOTIVE hard block — USE COMPOUND PHRASES to avoid false positives ──
    # NOTE: Removed standalone words that block legitimate auto content:
    #   - "election" blocked car auction/collection stories
    #   - "championship" blocked F1/MotoGP/WRC motorsport news
    #   - "series" blocked BMW 7 Series, C-series SUVs, racing series
    #   - "flood" blocked Mercedes Unimog off-road reviews
    #   - "drug" blocked DUI/driving safety articles
    #   - "theft" blocked stolen supercar stories (legitimate auto news)
    #   - "school" blocked AMG driving school/tuning articles
    #   - "exam" blocked car resale/evaluation articles
    #   - "movie" blocked automotive industry CEO interviews
    # Instead, use compound phrases that are clearly non-automotive:
    "market fire", "mall fire", "building fire", "warehouse fire",
    "shopping center fire", "store fire", "shop fire",
    "market burned", "mall burned", "store burned",
    "murder", "armed robbery", "violent crime",
    "narcotic trafficking", "drug cartel",
    "insurance fraud", "political corruption",
    "apartment fire", "house fire", "roof collapse", "pipe burst",
    "flood damage", "earthquake", "tsunami", "hurricane", "tornado warning",
    "weather forecast", "temperature ",
    "hospital", "doctor ", "surgery ", "patient ",
    "school shooting", "teacher strike", "school board",
    "football match", "soccer match", "hockey game", "olympic games",
    "movie review", "film festival", "theater production",
    "tv series", "streaming series",
    "restaurant review", "recipe ", "cooking ",
    "real estate market", "apartment price", "mortgage rate",
    "cryptocurrency", "bitcoin", "ethereum", "stock exchange",
    "plane crash", "airline crash", "train crash",
    "drowned", "beach resort", "swimming pool",
    "celebrity gossip", "celebrity scandal",
    "diet plan", "fitness program", "weight loss",
    "epidemic", "virus outbreak", "infection rate",
    "political protest", "political election", "general election",
    "parliament election", "presidential election",
]


# ── RSS Fetcher ────────────────────────────────────────────────────────────────

async def fetch_rss(source: NewsSource) -> List[Dict]:
    """
    Fetch and parse an RSS feed.
    Returns list of news items.
    """
    items = []
    try:
        async with httpx.AsyncClient(
            timeout=20.0,
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; AsyaBot/1.0; +https://t.me/asiaexp_bot)",
                "Accept": "application/rss+xml, application/xml, text/xml, application/atom+xml, */*",
            },
        ) as client:
            response = await client.get(source.url)

            if response.status_code != 200:
                logger.warning(f"RSS {source.name} returned {response.status_code}")
                return items

            # feedparser can parse from string
            feed = feedparser.parse(response.text)

            for entry in feed.entries[:20]:  # Limit per source
                title = entry.get("title", "").strip()
                url = entry.get("link", "").strip()

                if not title or not url:
                    continue

                # Get summary/description
                summary = ""
                if entry.get("summary"):
                    summary = _clean_html(entry["summary"])
                elif entry.get("description"):
                    summary = _clean_html(entry["description"])

                # Truncate summary
                if len(summary) > 500:
                    summary = summary[:500] + "..."

                # Get publish date
                published = 0.0
                if entry.get("published_parsed"):
                    try:
                        published = time.mktime(entry["published_parsed"])
                    except (ValueError, TypeError, OverflowError):
                        published = time.time()
                elif entry.get("updated_parsed"):
                    try:
                        published = time.mktime(entry["updated_parsed"])
                    except (ValueError, TypeError, OverflowError):
                        published = time.time()
                else:
                    published = time.time()

                # Extract image URLs from RSS entry
                image_urls = _extract_entry_images(entry)

                items.append({
                    "source": source.name,
                    "title": title,
                    "url": url,
                    "summary": summary,
                    "published": published,
                    "category": source.category,
                    "lang": source.lang,
                    "image_urls": image_urls,
                })

            logger.info(f"Fetched {len(items)} items from {source.name}")

    except httpx.TimeoutException:
        logger.warning(f"RSS timeout: {source.name}")
    except Exception as e:
        logger.error(f"RSS fetch error for {source.name}: {e}")

    return items


# ── News relevance filtering ───────────────────────────────────────────────────

def is_auto_relevant(item: Dict, lang: str = "ru") -> bool:
    """Check if a news item is relevant to the auto topic."""
    text = f"{item.get('title', '')} {item.get('summary', '')}".lower()

    if lang == "ru" or lang == "":
        keywords = AUTO_KEYWORDS_RU
    elif lang == "en":
        keywords = AUTO_KEYWORDS_EN
    elif lang == "de":
        keywords = AUTO_KEYWORDS_DE
    else:
        keywords = AUTO_KEYWORDS_RU + AUTO_KEYWORDS_EN

    # Check if any keyword matches
    for kw in keywords:
        if kw.lower() in text:
            return True

    return False


def is_blocked_topic(item: Dict, lang: str = "ru") -> bool:
    """Check if a news item contains blocked topics (politics, war, etc.).
    
    Returns True if the item should be REJECTED.
    This is a hard filter — items matching these keywords never enter the DB.
    """
    text = f"{item.get('title', '')} {item.get('summary', '')}".lower()
    
    # Check Russian blocklist (always checked for Russian and general sources)
    for kw in BLOCK_KEYWORDS_RU:
        if kw.lower() in text:
            logger.info(f"Blocked news (RU keyword '{kw}'): {item.get('title', '')[:60]}")
            return True
    
    # Check English blocklist (for international sources)
    if lang == "en":
        for kw in BLOCK_KEYWORDS_EN:
            if kw.lower() in text:
                logger.info(f"Blocked news (EN keyword '{kw}'): {item.get('title', '')[:60]}")
                return True
    
    return False


# ── Additional global RSS sources ──────────────────────────────────────────────
GLOBAL_RSS_SOURCES = [
    # World auto news
    {"name": "Reuters Auto", "url": "https://www.reuters.com/business/autos-transportation/rss", "lang": "en", "category": "auto"},
    {"name": "Motor1", "url": "https://www.motor1.com/rss/", "lang": "en", "category": "auto"},
    {"name": "CarNewsChina", "url": "https://carnewschina.com/feed/", "lang": "en", "category": "auto"},
    {"name": "Electrek", "url": "https://electrek.co/feed/", "lang": "en", "category": "auto"},
    {"name": "InsideEVs", "url": "https://insideevs.com/rss/", "lang": "en", "category": "auto"},
    # Reddit (community stories, mechanic advice, funny car moments)
    {"name": "Reddit r/cars", "url": "https://www.reddit.com/r/cars/.rss", "lang": "en", "category": "auto"},
    {"name": "Reddit r/MechanicAdvice", "url": "https://www.reddit.com/r/MechanicAdvice/.rss", "lang": "en", "category": "auto"},
    {"name": "Reddit r/Justrolledintotheshop", "url": "https://www.reddit.com/r/Justrolledintotheshop/.rss", "lang": "en", "category": "auto"},
]


async def fetch_global_rss_sources() -> List[Dict]:
    """Fetch news from additional global RSS sources.
    
    Returns list of news items with the same format as fetch_rss().
    """
    items = []
    for source_data in GLOBAL_RSS_SOURCES:
        source = NewsSource(
            name=source_data["name"],
            url=source_data["url"],
            lang=source_data.get("lang", "en"),
            category=source_data.get("category", "auto"),
        )
        try:
            source_items = await fetch_rss(source)
            for item in source_items:
                # Apply same filters
                if is_blocked_topic(item, source.lang):
                    continue
                if not is_auto_relevant(item, source.lang):
                    continue
                items.append(item)
            logger.info(f"Global RSS {source.name}: {len(source_items)} items")
        except Exception as e:
            logger.debug(f"Global RSS fetch error for {source.name}: {e}")
    return items


async def fetch_google_news_rss_batch() -> List[Dict]:
    """Fetch news from Google News RSS using multiple queries for broad coverage.
    
    Uses different queries and regional settings for global perspective.
    """
    items = []
    seen_urls = set()
    
    # Google News RSS queries for global coverage
    queries = [
        # Russian
        ("автомобили новости", "ru", "RU"),
        ("автозапчасти рынок", "ru", "RU"),
        ("китайские автомобили экспорт", "ru", "RU"),
        ("электромобили зарядные станции", "ru", "RU"),
        ("ПДД изменения штрафы", "ru", "RU"),
        # English
        ("automotive industry news", "en", "US"),
        ("electric vehicles latest", "en", "US"),
        ("autonomous driving self driving car", "en", "US"),
        ("car recalls safety alert", "en", "US"),
        ("auto show reveals 2025", "en", "US"),
        ("hydrogen fuel cell vehicle news", "en", "US"),
        ("EV battery technology", "en", "US"),
        ("Chinese cars global expansion", "en", "US"),
        ("used car market prices", "en", "US"),
        # German
        ("Auto Nachrichten Elektroauto", "de", "DE"),
    ]
    
    # Pick 4 random queries per cycle (don't fetch all at once)
    import random
    selected = random.sample(queries, min(4, len(queries)))
    
    for query, lang, gl in selected:
        try:
            from urllib.parse import quote_plus
            url = f"https://news.google.com/rss/search?q={quote_plus(query)}&hl={lang}&gl={gl}&ceid={gl}:{lang}"
            source = NewsSource(name=f"GoogleNews_{lang}", url=url, lang=lang, category="auto")
            source_items = await fetch_rss(source)
            for item in source_items:
                if item["url"] in seen_urls:
                    continue
                seen_urls.add(item["url"])
                if is_blocked_topic(item, lang):
                    continue
                if not is_auto_relevant(item, lang):
                    continue
                items.append(item)
        except Exception as e:
            logger.debug(f"Google News RSS failed for query '{query}': {e}")
    
    logger.info(f"Google News RSS batch: {len(items)} items")
    return items


# ── Main news fetching cycle ───────────────────────────────────────────────────

async def fetch_all_news() -> int:
    """
    Fetch news — WEB SEARCH FIRST, then RSS as fallback/supplement.
    Returns the number of new items added.
    
    Phase 1: Web search for automotive news (broad queries, primary source)
    Phase 2: RSS feeds (fallback/supplement)
    Phase 3: Global RSS sources (additional international coverage)
    Phase 4: Google News RSS batch (broad query-based coverage)
    
    All existing filtering is preserved (auto-relevance, political block, dedup).
    """
    total_new = 0

    # DO NOT reset fingerprint set every cycle!
    # Previously this caused the same news to be re-added across cycles.
    # The _recent_fingerprints set now persists across fetch cycles within the process.
    # DB-level dedup (is_duplicate_post) provides cross-restart protection.

    # ── Phase 1: Web Search FIRST (primary source) ──
    logger.info("Phase 1: Web search for automotive news")
    web_search_queries = [
        "automotive industry news today",
        "new car models 2026",
        "electric vehicle updates",
        "автомобильные новости России",
        "новые модели авто 2026",
        "car recalls and safety",
        "auto show reveals 2026",
        "автоновости сегодня",
        "electric vehicle news latest",
        "car industry updates",
    ]
    try:
        from bot.content_engine import search_auto_news
        # search_auto_news already uses multiple rotated queries
        search_items = await search_auto_news()
        for item in search_items:
            # Apply same filters as RSS
            if is_blocked_topic(item, item.get("lang", "en")):
                continue
            if not is_auto_relevant(item, item.get("lang", "en")):
                continue
            if await is_duplicate_post(item["title"], hours=48):
                continue
            fp = _compute_fingerprint(item["title"])
            if _fingerprint_matches_existing(fp):
                continue

            added = await add_news_item(
                source=item["source"],
                title=item["title"],
                url=item["url"],
                summary=item.get("summary", ""),
                published=item.get("published", time.time()),
                category=item.get("category", "auto"),
                lang=item.get("lang", "en"),
                image_urls=item.get("image_urls", []),
            )
            if added:
                total_new += 1
                _recent_fingerprints.add(fp)
                logger.info(f"Web search added: {item['title'][:60]}")
    except Exception as e:
        logger.warning(f"Web search phase failed: {e}")

    logger.info(f"Phase 1 (web search) complete: {total_new} new items")

    # ── Phase 2: RSS Feeds (fallback/supplement) ──
    logger.info("Phase 2: RSS feeds (fallback/supplement)")
    for source in news_config.sources:
        try:
            items = await fetch_rss(source)

            for item in items:
                # Hard filter: block political/war topics FIRST
                if is_blocked_topic(item, source.lang):
                    continue

                # Filter for auto relevance (ALL sources checked — even "auto" feeds can carry non-auto items)
                if not is_auto_relevant(item, source.lang):
                    continue

                # Filter out news with titles similar to recently posted items
                if await is_duplicate_post(item["title"], hours=48):
                    logger.debug(f"Skipping duplicate news title: {item['title'][:60]}")
                    continue

                # Fingerprint-based dedup: skip if similar to recently added news
                fp = _compute_fingerprint(item["title"])
                if _fingerprint_matches_existing(fp):
                    logger.debug(f"Skipping similar news (fingerprint match): {item['title'][:60]}")
                    continue

                # Add to database (dedup by URL)
                added = await add_news_item(
                    source=item["source"],
                    title=item["title"],
                    url=item["url"],
                    summary=item["summary"],
                    published=item["published"],
                    category=item["category"],
                    lang=item["lang"],
                    image_urls=item.get("image_urls", []),
                )
                if added:
                    total_new += 1
                    _recent_fingerprints.add(fp)

        except Exception as e:
            logger.error(f"Error processing source {source.name}: {e}")
            continue

    # ── Phase 3: Global RSS sources ──
    logger.info("Phase 3: Global RSS sources")
    try:
        global_items = await fetch_global_rss_sources()
        for item in global_items:
            if is_blocked_topic(item, item.get("lang", "en")):
                continue
            if not is_auto_relevant(item, item.get("lang", "en")):
                continue
            if await is_duplicate_post(item["title"], hours=48):
                continue
            fp = _compute_fingerprint(item["title"])
            if _fingerprint_matches_existing(fp):
                continue
            added = await add_news_item(
                source=item["source"],
                title=item["title"],
                url=item["url"],
                summary=item.get("summary", ""),
                published=item.get("published", time.time()),
                category=item.get("category", "auto"),
                lang=item.get("lang", "en"),
                image_urls=item.get("image_urls", []),
            )
            if added:
                total_new += 1
                _recent_fingerprints.add(fp)
                logger.info(f"Global RSS added: {item['title'][:60]}")
    except Exception as e:
        logger.warning(f"Global RSS phase failed: {e}")
    
    logger.info(f"Phase 3 (global RSS) complete: {total_new} total new items")

    # ── Phase 4: Google News RSS batch ──
    logger.info("Phase 4: Google News RSS batch")
    try:
        gn_items = await fetch_google_news_rss_batch()
        for item in gn_items:
            if is_blocked_topic(item, item.get("lang", "en")):
                continue
            if not is_auto_relevant(item, item.get("lang", "en")):
                continue
            if await is_duplicate_post(item["title"], hours=48):
                continue
            fp = _compute_fingerprint(item["title"])
            if _fingerprint_matches_existing(fp):
                continue
            added = await add_news_item(
                source=item["source"],
                title=item["title"],
                url=item["url"],
                summary=item.get("summary", ""),
                published=item.get("published", time.time()),
                category=item.get("category", "auto"),
                lang=item.get("lang", "en"),
                image_urls=item.get("image_urls", []),
            )
            if added:
                total_new += 1
                _recent_fingerprints.add(fp)
                logger.info(f"Google News RSS added: {item['title'][:60]}")
    except Exception as e:
        logger.warning(f"Google News RSS phase failed: {e}")
    
    logger.info(f"Phase 4 (Google News RSS) complete: {total_new} total new items")

    logger.info(f"News fetch complete: {total_new} new items")
    return total_new


# ── International news with translation context ────────────────────────────────

async def fetch_international_auto_news() -> List[Dict]:
    """
    Fetch international auto news and prepare for translation.
    Returns items that need translation (non-Russian).
    """
    international_items = []

    for source in news_config.sources:
        if source.lang != "ru":
            try:
                items = await fetch_rss(source)
                for item in items:
                    if is_auto_relevant(item, source.lang):
                        international_items.append(item)
            except Exception as e:
                logger.error(f"Error fetching international news from {source.name}: {e}")

    return international_items


# ── News cycle runner ──────────────────────────────────────────────────────────

async def run_news_cycle() -> int:
    """
    Run a complete news cycle:
    1. Fetch from all sources
    2. Filter for relevance
    3. Store in DB
    
    Returns count of new items.
    """
    logger.info("Starting news fetch cycle...")
    count = await fetch_all_news()
    logger.info(f"News cycle complete: {count} new items")
    return count


# ── Image extraction from RSS entries ──────────────────────────────────────────

# Image extensions that are valid for Telegram posts
_VALID_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
# Domains to skip (icons, trackers, tiny thumbnails)
_SKIP_IMAGE_DOMAINS = {
    "feeds.feedburner.com", "feedburner.google.com",
    "pixel.wp.com", "stats.wordpress.com",
    "i0.wp.com", "i1.wp.com", "i2.wp.com",
}
# Minimum image URL length (filter out tiny 1x1 tracking pixels etc.)
_MIN_IMAGE_URL_LEN = 30


def _extract_entry_images(entry) -> List[str]:
    """Extract image URLs from a feedparser entry.
    
    Checks multiple locations where RSS/Atom feeds store images:
    - media_content (Media RSS)
    - enclosures
    - media_thumbnail
    - links with image type
    - content/summary HTML <img> tags
    
    Returns list of image URLs (up to 10).
    """
    images = []
    seen = set()

    def _add_image(url: str):
        """Add image URL if valid and not already seen."""
        if not url or len(url) < _MIN_IMAGE_URL_LEN:
            return
        # Normalize URL
        url = url.strip()
        if url.startswith("//"):
            url = "https:" + url
        # Skip tracking pixels and tiny icons
        from urllib.parse import urlparse
        try:
            parsed = urlparse(url)
            domain = parsed.netloc.lower()
            if any(skip in domain for skip in _SKIP_IMAGE_DOMAINS):
                return
            # Skip very small-looking URLs (often icons/pixels)
            path_lower = parsed.path.lower()
            if any(kw in path_lower for kw in ["icon", "avatar", "logo", "favicon", "pixel", "badge", "button", "banner"]):
                return
        except Exception:
            return
        if url not in seen:
            seen.add(url)
            images.append(url)

    # 1. media_content (Media RSS extension — most reliable)
    media_content = entry.get("media_content", [])
    for media in media_content:
        url = media.get("url", "")
        media_type = media.get("type", "")
        if url and ("image" in media_type or _has_image_ext(url)):
            _add_image(url)

    # 2. enclosures
    for enclosure in entry.get("enclosures", []):
        url = enclosure.get("href", "") or enclosure.get("url", "")
        enc_type = enclosure.get("type", "")
        if url and ("image" in enc_type or _has_image_ext(url)):
            _add_image(url)

    # 3. media_thumbnail
    for thumb in entry.get("media_thumbnail", []):
        url = thumb.get("url", "")
        if url:
            _add_image(url)

    # 4. links with image rel or type
    for link in entry.get("links", []):
        link_type = link.get("type", "")
        link_rel = link.get("rel", "")
        url = link.get("href", "")
        if url and ("image" in link_type or link_rel == "enclosure"):
            _add_image(url)

    # 5. Extract <img> tags from content/summary HTML
    for field_name in ["content", "summary", "description"]:
        content_value = entry.get(field_name, [])
        if isinstance(content_value, list):
            # feedparser sometimes returns list of dicts
            for item in content_value:
                html = item.get("value", "") if isinstance(item, dict) else str(item)
                _extract_img_urls(html, _add_image)
        elif isinstance(content_value, str):
            _extract_img_urls(content_value, _add_image)

    return images[:10]  # Up to 10 candidate images per news item (Telegram mediagroup limit)


def _has_image_ext(url: str) -> bool:
    """Check if URL ends with an image extension."""
    from urllib.parse import urlparse
    try:
        path = urlparse(url).path.lower()
        # Remove query-like suffixes
        path = path.split("?")[0].split("#")[0]
        return any(path.endswith(ext) for ext in _VALID_IMAGE_EXTENSIONS)
    except Exception:
        return False


def _extract_img_urls(html: str, callback):
    """Extract src attributes from <img> tags in HTML."""
    if not html:
        return
    for match in re.finditer(r'<img[^>]+src=["\']([^"\']+)["\']', html, re.IGNORECASE):
        url = match.group(1)
        if url and len(url) >= _MIN_IMAGE_URL_LEN:
            callback(url)


# ── Utility ────────────────────────────────────────────────────────────────────

def _clean_html(text: str) -> str:
    """Remove HTML tags and decode entities."""
    text = re.sub(r'<[^>]+>', '', text)
    text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
    text = text.replace('&quot;', '"').replace('&#39;', "'").replace('&nbsp;', ' ')
    text = re.sub(r'\s+', ' ', text).strip()
    return text
