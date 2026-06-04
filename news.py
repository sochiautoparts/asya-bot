"""
News Engine — AUTO-FOCUSED RSS fetching, international news + translation.
Fetches from multiple sources, deduplicates, stores in DB.
"""

import feedparser
import httpx
import time
import logging
import re
from typing import List, Dict, Optional
from datetime import datetime

from bot.config import config, news_config, NewsSource
from bot.database import add_news_item, get_unposted_news, mark_news_posted

logger = logging.getLogger("asya.news")

# ── Keywords for auto-relevance filtering ──────────────────────────────────────

AUTO_KEYWORDS_RU = [
    "авто", "автомобиль", "машина", "мотор", "двигатель", "кузов", "салон",
    "шоссе", "дорог", "скорост", "транспорт", "запчас", "ремонт", "сервис",
    "шин", "колес", "топлив", "бензин", "дизел", "электромобиль", "гибрид",
    "тест-драйв", "обзор", "новинка", "модель", "бренд", "марка",
    "продаж", "рынок", "стоимост", "цен", "акциз", "таможен",
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
]

AUTO_KEYWORDS_DE = [
    "Auto", "Automobil", "Fahrzeug", "Motor", "Antrieb", "Straße",
    "Reparatur", "Service", "Reifen", "Benzin", "Diesel", "Elektro",
    "Neuvorstellung", "Modell", "Marke", "Verkauf", "Preis",
    "Getriebe", "Federung", "Bremse", "Lenkung",
    "Renn", "Formel", "SUV", "Limousine",
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

                items.append({
                    "source": source.name,
                    "title": title,
                    "url": url,
                    "summary": summary,
                    "published": published,
                    "category": source.category,
                    "lang": source.lang,
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


# ── Main news fetching cycle ───────────────────────────────────────────────────

async def fetch_all_news() -> int:
    """
    Fetch news from all configured sources.
    Returns the number of new items added.
    """
    total_new = 0

    for source in news_config.sources:
        try:
            items = await fetch_rss(source)

            for item in items:
                # Filter for auto relevance (skip general news that aren't auto-related)
                if source.category == "general" and not is_auto_relevant(item, source.lang):
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
                )
                if added:
                    total_new += 1

        except Exception as e:
            logger.error(f"Error processing source {source.name}: {e}")
            continue

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


# ── Utility ────────────────────────────────────────────────────────────────────

def _clean_html(text: str) -> str:
    """Remove HTML tags and decode entities."""
    text = re.sub(r'<[^>]+>', '', text)
    text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
    text = text.replace('&quot;', '"').replace('&#39;', "'").replace('&nbsp;', ' ')
    text = re.sub(r'\s+', ' ', text).strip()
    return text
