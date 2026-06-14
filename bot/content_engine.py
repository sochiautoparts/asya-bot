"""Smart Content Engine v3.0 — Single-Source JSON Pipeline for @sochiautoparts.

ARCHITECTURE (v3.0 — Single Source):
  Phase 1: FETCH — News from creastudioai-beep/news JSON (external parser)
  Phase 2: SCORE — Interest scoring + image bonus + freshness
  Phase 3: AI PICK — AI selects the BEST topic from top 5 candidates
  Phase 4: DEDUPLICATE — Topic registry with entity extraction
  Phase 5: ENRICH — Translation + unique text + expert opinion (via AI post generation)
  Phase 6: POST — Quality validation with interest scoring

KEY CHANGES FROM v2.0:
  - NO RSS PARSING — external parser handles all RSS/HTML/image extraction
  - NO WEB SEARCH — external parser provides pre-curated news
  - NO AI DISCOVERY — external parser runs every hour with 20+ sources
  - Images come directly from JSON (pre-extracted by external parser)
  - Bot only: fetches JSON → scores → picks best → generates post → publishes

KEPT FROM v2.0:
  - Topic registry prevents duplicate coverage of same event
  - AI interest scoring — skip boring/technical news nobody reads
  - Entity extraction — brand, model, event tracking for smart dedup
  - Date context — Ася always knows what year it is!
  - Tone analysis — appropriate jokes for different news types
  - Translation & uniquification hints for AI
  - Editorial team personality (Лёха, Димон, Марина, Кеша, Сеньор Помидор)
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

from bot.config import config, persona

logger = logging.getLogger("asya.content_engine")

# ── Topic Registry — persistent dedup by entities/events ──────────────────────
# Maps: entity_key → {first_seen, last_posted, post_count, titles}
_topic_registry: Dict[str, Dict] = {}
_REGISTRY_MAX_AGE_HOURS = 72  # Keep topics for 72 hours

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

# Notable people / F1 drivers for entity extraction
_NOTABLE_PEOPLE = [
    "Alonso", "Hamilton", "Verstappen", "Vettel", "Leclerc", "Norris",
    "Sainz", "Russell", "Perez", "Piastri", "Ricciardo", "Stroll",
    "Ocon", "Gasly", "Tsunoda", "Albon", "Bottas", "Hulkenberg",
    "Musk", "Маск", "Шумахер", "Сенна",
]

# F1 / motorsport teams
_MOTORSPORT_TEAMS = [
    "Red Bull", "Ferrari", "Mercedes", "McLaren", "Aston Martin",
    "Alpine", "Williams", "Haas", "RB", "Sauber",
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
]

# Moscow timezone
_MOSCOW_TZ = ZoneInfo("Europe/Moscow")


def _extract_entities(title: str) -> str:
    """Extract key entities from a news title for dedup.
    
    Returns a normalized entity key like "bmw_m5_reveal", "alonso_criticism", or "toyota_recalls".
    """
    title_lower = title.lower()
    
    # Extract brand
    brand = ""
    for b in _AUTO_BRANDS:
        if b.lower() in title_lower:
            brand = b.lower().replace(" ", "_")
            break
    
    # Extract notable person
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
    
    # Extract model
    model = ""
    if brand:
        model_patterns = [
            r'\b([mglxqsec]\d+)\b',
            r'\b(\d{3,4}[ix]?)\b',
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
    
    return entity_key


def _is_topic_covered(entity_key: str) -> bool:
    """Check if this topic/entity was already posted about recently.
    
    v2.1: Reduced coverage window. Was blocking same entity for 24-72h,
    now only blocks for 12h. Different events about the same car model
    (e.g., "bmw_m5_launch" vs "bmw_m5_recall") should BOTH be posted.
    Only block if it's truly the SAME event posted recently.
    """
    if not entity_key:
        return False
    
    now = time.time()
    
    if entity_key in _topic_registry:
        entry = _topic_registry[entity_key]
        age = now - entry["last_posted"]
        # v2.1: Only block if posted within 12h (was 24h — too aggressive)
        # Same entity within 12h = likely same news being re-fetched
        if age < 12 * 3600:
            return True
        # 12-72h ago — only block if posted 3+ times (was 2 — too strict)
        if age < _REGISTRY_MAX_AGE_HOURS * 3600 and entry["post_count"] >= 3:
            return True
        if age >= _REGISTRY_MAX_AGE_HOURS * 3600:
            # Old entry — remove
            del _topic_registry[entity_key]
    
    return False


def _register_topic(entity_key: str, title: str):
    """Register that this topic was posted about."""
    if not entity_key:
        return
    
    now = time.time()
    if entity_key in _topic_registry:
        _topic_registry[entity_key]["post_count"] += 1
        _topic_registry[entity_key]["last_posted"] = now
        _topic_registry[entity_key]["titles"].append(title)
        # Keep only last 5 titles
        _topic_registry[entity_key]["titles"] = _topic_registry[entity_key]["titles"][-5:]
    else:
        _topic_registry[entity_key] = {
            "first_seen": now,
            "last_posted": now,
            "post_count": 1,
            "titles": [title],
        }
    
    # Brand-only registration — DISABLED in v2.1
    # Was registering brand-only keys (e.g., "bmw") when posting about "bmw_m5_reveal",
    # which then blocked ALL other BMW news for 24-72 hours.
    # Entity-level dedup (brand+model+event) is sufficient without brand-only blocking.
    

def _cleanup_registry():
    """Remove old entries from topic registry."""
    now = time.time()
    max_age = _REGISTRY_MAX_AGE_HOURS * 3600
    expired = [k for k, v in _topic_registry.items() if now - v["last_posted"] > max_age]
    for k in expired:
        del _topic_registry[k]
    if expired:
        logger.debug(f"Cleaned up {len(expired)} expired topic registry entries")


def _score_interest(title: str, summary: str = "") -> float:
    """Score news interest level (0.0 to 1.0+).
    
    Higher score = more interesting for channel audience.
    Factors: brand popularity, event type, emotional keywords.
    """
    text = f"{title} {summary}".lower()
    score = 0.4  # Base score
    
    # Premium/sports brands = more interest
    premium_brands = ["ferrari", "lamborghini", "porsche", "mclaren", "bugatti", "aston martin",
                      "bentley", "rolls-royce", "maserati", "lotus", "tesla"]
    for brand in premium_brands:
        if brand in text:
            score += 0.3
            break
    
    # Popular brands = moderate interest
    popular_brands = ["bmw", "mercedes", "audi", "toyota", "honda", "porsche",
                      "lexus", "volvo", "hyundai", "kia", "byd", "zeekr",
                      "geely", "chery", "haval", "changan", "exeed"]
    for brand in popular_brands:
        if brand in text:
            score += 0.15
            break
    
    # Chinese brands in Russia = high interest for RU audience
    chinese_brands = ["byd", "zeekr", "geely", "chery", "haval", "changan", "exeed",
                      "tank", "li auto", "nio", "omicron", "jaecoo", "jetour"]
    for brand in chinese_brands:
        if brand in text:
            score += 0.2
            break
    
    # Exciting events = more interest
    exciting_events = [
        "reveal", "debut", "launch", "unveil", "премьер", "дебют",
        "record", "рекорд", "breakthrough", "прорыв",
    ]
    for event in exciting_events:
        if event in text:
            score += 0.2
            break
    
    # Negative events = moderate interest (recalls, bans)
    negative_events = ["recall", "отзыв", "ban", "запрет", "crash", "авария"]
    for event in negative_events:
        if event in text:
            score += 0.15
            break
    
    # Boring events = less interest
    boring_events = ["report", "отчёт", "quarterly", "earnings", "прибыл",
                     "investment", "инвестиц", "factory", "завод", "plant"]
    for event in boring_events:
        if event in text:
            score -= 0.15
            break
    
    # Emotional keywords = more engagement
    emotional_kw = ["shocking", "incredible", "unbelievable", "historic",
                    "невероятн", "шокиру", "историче", "скандал", "сенсаци"]
    for kw in emotional_kw:
        if kw in text:
            score += 0.15
            break
    
    # Electric/EV = trendy
    ev_kw = ["electric", "ev ", "battery", "электриче", "электромобил", "заряд"]
    for kw in ev_kw:
        if kw in text:
            score += 0.1
            break
    
    return max(0.0, min(1.5, score))


def _score_freshness(published_time) -> float:
    """Score freshness bonus based on publication time."""
    if not published_time:
        return 0.0
    
    try:
        if isinstance(published_time, (int, float)):
            age_hours = (time.time() - published_time) / 3600
        elif isinstance(published_time, str):
            # ISO format
            from datetime import datetime
            try:
                pub_dt = datetime.fromisoformat(published_time.replace("Z", "+00:00"))
                age_hours = (datetime.now(_MOSCOW_TZ) - pub_dt).total_seconds() / 3600
            except Exception:
                return 0.0
        else:
            return 0.0
        
        if age_hours < 1:
            return 0.5  # Very fresh
        elif age_hours < 3:
            return 0.3
        elif age_hours < 6:
            return 0.2
        elif age_hours < 12:
            return 0.1
        elif age_hours < 24:
            return 0.0
        else:
            return -0.3  # Stale
    except Exception:
        return 0.0


async def get_best_news_item(unposted_items: List[Dict]) -> Optional[Dict]:
    """Select the best news item to post from pre-fetched JSON data.
    
    Pipeline (v3.0 — Single Source):
    1. Score all items for interest
    2. Extract entities and check topic registry
    3. Filter out covered topics
    4. Take top 5 candidates → AI picks the BEST one
    5. Return selected item
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
    
    if not unposted_items:
        logger.info("No unposted items available")
        return None
    
    # ── SCORE & DEDUPLICATE ──
    scored_items = []
    seen_titles = set()
    
    now_ts = time.time()
    
    for item in unposted_items:
        title = item.get("title", "")
        summary = item.get("summary", "")
        
        # Skip exact title duplicates
        title_lower = title.lower().strip()
        if title_lower in seen_titles:
            continue
        seen_titles.add(title_lower)
        
        # ── DB fingerprint check ──
        try:
            from bot.database import is_duplicate_post
            if await is_duplicate_post(title, hours=72):
                logger.debug(f"DB dedup blocked: {title[:50]}")
                continue
        except Exception:
            pass
        
        # Extract entities for dedup
        entity_key = _extract_entities(title)
        
        # Check if topic is already covered
        if _is_topic_covered(entity_key):
            logger.debug(f"Topic already covered: {entity_key} — {title[:50]}")
            continue
        
        # Brand-only dedup — DISABLED in v2.1
        # Was blocking ALL news about a brand after just 1 post about it.
        # Example: "BMW recalls X5" blocked "BMW launches new M3" — different events!
        # Entity-level dedup (_is_topic_covered with entity_key) is sufficient.
        # Brand-only dedup was causing the bot to skip 80%+ of available news.
        
        # Score interest
        interest = _score_interest(title, summary)
        
        # Freshness bonus
        published = item.get("published", "") or item.get("published_time", 0)
        freshness_bonus = _score_freshness(published)
        interest += freshness_bonus
        
        # BONUS for having photos — items WITH images get MASSIVE priority
        # v3.1: Increased bonuses significantly — photos are CRITICAL for engagement
        image_count = len(item.get("image_urls", []))
        if image_count >= 5:
            interest += 2.0  # Was 1.0 — rich gallery = best engagement
        elif image_count >= 3:
            interest += 1.5  # Was 0.7 — multiple photos = great
        elif image_count >= 1:
            interest += 1.0  # Was 0.4 — at least one photo = good
        else:
            interest -= 0.5  # Was -0.3 — text-only = significantly less engaging
        
        scored_items.append({
            "item": item,
            "interest": interest,
            "entity_key": entity_key,
        })
    
    if not scored_items:
        logger.info("No fresh topics from JSON source — skipping this cycle")
        return None
    
    # Sort by interest score
    scored_items.sort(key=lambda x: x["interest"], reverse=True)
    
    # v3.1: Secondary sort — among items with similar interest, prefer those with photos
    # This ensures photos always win, even if text-only items have slightly higher base scores
    def _sort_key(entry):
        interest = entry["interest"]
        img_count = len(entry["item"].get("image_urls", []))
        has_images = 1 if img_count > 0 else 0
        return (has_images, interest)
    
    scored_items.sort(key=_sort_key, reverse=True)
    
    # ── AI picks the BEST from top 10 (was 5 — expanded for better selection) ──
    top_n = scored_items[:min(10, len(scored_items))]
    
    if len(top_n) == 1:
        chosen = top_n[0]
    else:
        # Let AI pick the most interesting one
        try:
            from ai.router import ai_router
            candidates_summary = []
            for i, entry in enumerate(top_n):
                item = entry["item"]
                img_count = len(item.get("image_urls", []))
                photos_str = f" [+{img_count}📷]" if img_count > 0 else " [no📷]"
                candidates_summary.append(
                    f"{i+1}. [{entry['interest']:.2f}]{photos_str} {item.get('title', '')[:100]}"
                )
            
            candidates_text = "\n".join(candidates_summary)
            
            _TOPIC_PICK_MODELS = ["mistral-4", "deepseek", "qwen-large"]
            
            chosen = top_n[0]  # Default
            for model_name in _TOPIC_PICK_MODELS:
                try:
                    response = await ai_router._primary.chat(
                        messages=[
                            {"role": "system", "content": (
                                "Ты редактор автоканала в Telegram. Тебе даны кандидаты на публикацию "
                                "с оценкой интереса и количеством фотографий (📷). "
                                "Выбери САМЫЙ интересный для широкой аудитории — "
                                "то, что вызовет наибольший отклик и обсуждение. "
                                "ПРИОРИТЕТ: новости С фото (📷) лучше чем без — "
                                "фотографии критически важны для вовлечения! "
                                "НЕ выбирай новости про АвтоВАЗ/LADA/УАЗ — скучно. "
                                "Ответь ТОЛЬКО цифрой — номер лучшего кандидата."
                            )},
                            {"role": "user", "content": f"Кандидаты:\n{candidates_text}"},
                        ],
                        model=model_name,
                        temperature=0.3,
                        max_tokens=5,
                    )
                    
                    if not response.error and response.text and response.text.strip():
                        pick_match = re.search(r'[1-9]|10', response.text.strip())
                        if pick_match:
                            pick_idx = int(pick_match.group()) - 1
                            if 0 <= pick_idx < len(top_n):
                                chosen = top_n[pick_idx]
                                logger.info(f"AI picked #{pick_idx + 1}: {chosen['item'].get('title', '')[:60]}")
                                break  # Only break on successful pick
                except Exception:
                    continue
        except Exception as e:
            logger.debug(f"AI topic selection failed, using top-1: {e}")
            chosen = top_n[0]
    
    best_item = chosen["item"]
    
    logger.info(
        f"Selected news: interest={chosen['interest']:.2f}, entity={chosen.get('entity_key', 'none')}, "
        f"title={best_item.get('title', '')[:60]}"
    )
    
    # NOTE: Do NOT register topic here!
    # Topic registration happens in channel.py AFTER the post is actually published.
    
    return best_item


def get_editorial_team_comment() -> str:
    """Get a random dialogue/comment from the editorial team characters."""
    team_comments = [
        # Ася
        "Я на своей Quadrifoglio уже на третьем комплекте колодок за год — но я и не медленно езжу",
        "Звук V6 лечит головную боль. Научных подтверждений нет, но редакция верит",
        "На моей Альфе всё чинится в три раза дороже — и это я ещё экономлю",
        "Ася запретила шутки про Alfa Romeo — теперь шутим тихо",
        "Ася прочитала черновик и молча ушла. Вернулась с эспрессо и правками",
        # Лёха
        "Оригинал? Это тот же Lemförder с логотипом за двойную цену",
        "Лёха глянул на новые фары и спросил: а лампочки-то менять как? Через экран?",
        "Лёха чинит всё что сломано, и ломает всё что работало",
        "Лёха смотрит на электромобиль как на личное оскорбление",
        # Димон
        "Димон предлагает внедрить AI в диагностику. Лёха предлагает внедрить кувалду",
        "Димон обновил всё — и теперь ничего не работает. Зато безопасно!",
        "У Димона 47 вкладок открыто — и он утверждает что все нужны",
        # Марина
        "Марина посчитала стоимость владения и ушла плакать. Вернулась с купоном на Росско",
        "Мне всё равно какой 0-100. Мне важно: влезут ли два кресла и коляска",
        "Марина: 'А где расходники?' — вопрос, от которого всё замолкает",
        # Кеша (попугай)
        "Кеша с жёрдочки кричит 'Свободная пресса!'",
        "Кеша считает что BMW — это разновидность птицы",
        "Кеша уронил семечко на 'Отправить' — пост ушёл раньше времени",
        # Сеньор Помидор (кот)
        "Сеньор Помидор лёг на клавиатуру — пост прерывается на 'гвфдыаопр'",
        "Кот редакции внёс правки — удалил половину текста и уснул",
        "Кот проверил новость лапой — одобрено (потянулся и уснул)",
    ]
    return random.choice(team_comments)


def get_editorial_aside() -> str:
    """Get a random editorial aside/joke for channel posts.
    45% chance of returning empty for variety.
    """
    if random.random() < 0.45:
        return ""
    if random.random() < 0.5 and hasattr(persona, 'editorial_asides') and persona.editorial_asides:
        return random.choice(persona.editorial_asides)
    return get_editorial_team_comment()


def get_translation_uniquification_hint(lang: str) -> str:
    """Get a hint for the AI about translating/uniquifying content.
    
    Uses a 7-step transformation process to ensure unique, high-quality content.
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
        "ПЕРЕПИШИ НОВОСТЬ ПОЛНОСТЬЮ СВОИМИ СЛОВАМИ. "
        "Это ОБЯЗАТЕЛЬНО — нельзя копировать фразы из источника. "
        "Измени структуру, начни с другого факта или эмоции. "
        "Добавь экспертное мнение от лица редакции, "
        "сравнение с другими марками/моделями, или контекст для аудитории. "
        "Пост должен быть уникальным авторским текстом редакции @sochiautoparts.\n\n"
        + _7_STEP_PROCESS
    )


def get_date_context() -> str:
    """Get current date/time context string for AI prompts."""
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


# ── TONE ANALYSIS ────────────────────────────────────────────────────────────

from enum import Enum
from dataclasses import dataclass

class NewsTone(Enum):
    SERIOUS = "serious"
    HYPE = "hype"
    ROUTINE = "routine"
    FUN = "fun"
    TECHNICAL = "technical"

@dataclass
class ExtractedFacts:
    brand: str
    model: str
    year: Optional[str]
    price: Optional[str]
    power: Optional[str]
    key_event: str
    tone: NewsTone
    is_partner: bool = False

async def analyze_news_tone(title: str, summary: str, content: str = "") -> ExtractedFacts:
    """Analyze news tone and extract facts for post generation."""
    text = f"{title} {summary} {content}".lower()
    
    # Extract brand
    brand = ""
    for b in _AUTO_BRANDS:
        if b.lower() in text:
            brand = b
            break
    
    # Extract model
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
    
    price_match = re.search(r'\$([\d,]+)', text)
    if price_match:
        price = price_match.group(1)
    else:
        price_match = re.search(r'([\d,]+)\s*(?:руб|рубл|rub|₽)', text, re.IGNORECASE)
        if price_match:
            price = price_match.group(1)
    
    power_match = re.search(r'(\d+)\s*(?:л\.?с|hp|horsepower|лс)', text, re.IGNORECASE)
    if power_match:
        power = power_match.group(1)
    
    year_match = re.search(r'\b(20[12]\d)\b', text)
    if year_match:
        year = year_match.group(1)
    
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
    
    serious_kw = ["дтп", "авария", "катастроф", "погиб", "смерть", "жертв",
                  "отзыв", "отзывают", "recalls", "бан", "запрет", "штраф",
                  "crash", "accident", "death", "fatal", "recall", "ban"]
    for kw in serious_kw:
        if kw in text_lower or kw in title_lower:
            return NewsTone.SERIOUS
    
    fun_kw = ["забавн", "курьез", "смешн", "необычн", "удивител",
              "funny", "curious", "weird", "strange", "amazing"]
    for kw in fun_kw:
        if kw in text_lower or kw in title_lower:
            return NewsTone.FUN
    
    hype_kw = ["премьер", "дебют", "анонс", "представлен", "новинк",
               "рекорд", "суперкар", "гиперкар", "прорыв",
               "reveal", "debut", "launch", "unveil", "record", "supercar"]
    for kw in hype_kw:
        if kw in text_lower or kw in title_lower:
            return NewsTone.HYPE
    
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
        return ""
    
    joke_pools = {
        NewsTone.HYPE: [
            "Редакция в шоке: даже кофе не бодрит так, как эта новость! ☕",
            "Ася чуть не уронила эспрессо, когда увидела эту новость! 😱",
            "Лёха отложил гаечный ключ — а он его НИКОГДА не откладывает 🔧",
            "Кеша даже перестал есть семечки — новость THAT хороша! 🦜",
            "Сеньор Помидор ПРОСНУЛСЯ — вот это реально редкость 🐱",
        ],
        NewsTone.ROUTINE: [
            "Пока варим утренний кофе, делимся новостью... ☕",
            "Сломали очередной карандаш, составляя этот пост ✏️",
            "Рутина — но с характером. Как утренний дедлайн ☀️",
            "Среда, рутина, новость. Редакция работает дальше 💪",
        ],
        NewsTone.FUN: [
            "В редакции смеялись до слез 😂",
            "Кеша танцует на жёрдочке — новость его развеселила 💃🦜",
            "Сеньор Помидор мурлычет — а он мурлычет только на хорошие новости 😻",
            "Лёха УЛЫБНУЛСЯ. Мы это запечатлили — редкий кадр! 😁",
        ],
        NewsTone.TECHNICAL: [
            "Разбираемся в цифрах, пока кофе остывает... ☕📊",
            "Димон в восторге от спецификаций — Лёха скептичен. Классика 🤓🔧",
            "Марина проверила расчёты — всё сходится. Редкость! 🧮",
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
