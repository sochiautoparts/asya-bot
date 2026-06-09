from bot.media_handler import media_handler, ImageQuality

Channel Manager — Posts to @sochiautoparts with proper formatting.
Handles news posts, partner posts, scheduled content, reactions,
media, polls, and internet news search.
Properly enforces Telegram character limits: 1024 with media, 4096 without.
Posts silently (disable_notification) matching channel settings.
"""

import logging
import time
import random
import asyncio
import tempfile
import os
import re
import hashlib
import httpx
import aiosqlite
from typing import Optional, List, Dict, Tuple
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.types import FSInputFile, ReactionTypeEmoji, InputMediaPhoto
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardButton

from bot.config import config, persona
from bot.database import (
    add_channel_post, get_today_post_count, get_hourly_post_count, get_unposted_news,
    mark_news_posted, add_partner_post, get_today_partner_post_count,
    is_duplicate_post, add_post_fingerprint, cleanup_old_fingerprints,
    get_recent_post_titles, DB_PATH,
)
from ai.router import ai_router
from bot.partners import partner_manager
from bot.web_search import web_search, search_news, SearchResult
from bot.content_engine import (
    get_best_news_item, enrich_with_search_images, get_date_context,
    _is_topic_covered, _extract_entities, _score_interest,
    _register_topic, get_editorial_aside, get_translation_uniquification_hint,
)
# Channel scanner removed — unreliable from GitHub Actions IPs (403/429).
# DB fingerprint + semantic dedup are sufficient.

logger = logging.getLogger("asya.channel")

# ── Reactions to add to posts ───────────────────────────────────────────────

POST_REACTIONS = ["👍", "🔥", "🚗", "😍", "👏", "💯", "🤩", "⚡"]

# ── How many images per news post ───────────────────────────────────────────
# Telegram allows up to 10 media per post.
# We aim for rich visual posts with multiple relevant images from news sources.
NEWS_IMAGES_MIN = 2
NEWS_IMAGES_MAX = 3
# Maximum total images in a channel post (hard limit)
MAX_IMAGES_PER_POST = 10
# Maximum real images to download from RSS (not the Telegram limit!)
MAX_RSS_IMAGES = 5
# Maximum images to scrape from article page
MAX_SCRAPE_IMAGES = 5
# Maximum images from web search enrichment
MAX_SEARCH_IMAGES = 5

# ── Poll topics for channel engagement ──────────────────────────────────────

POLL_TEMPLATES = [
    "Что думаете об этой новости?",
    "Ваше мнение?",
    "Как вам такая новость?",
    "Оцените новость!",
    "Что скажете?",
]

# Moscow timezone
_MOSCOW_TZ = ZoneInfo("Europe/Moscow")

# ── Keyword-based semantic dedup for channel posts ─────────────────────────────
# Stores significant words from recently posted titles
_recent_post_keywords: list = []
_MAX_RECENT_POSTS = 100  # Track more posts for better semantic dedup (48 posts/day)

# Words to ignore in semantic comparison
_SEMANTIC_STOP_WORDS = frozenset([
    "в", "на", "с", "о", "у", "по", "из", "за", "от", "до", "к", "не", "и", "но",
    "а", "что", "как", "это", "тот", "этот", "для", "при", "через", "между",
    "после", "перед", "без", "под", "над", "об", "со", "то", "же", "ли", "бы",
    "уже", "ещё", "еще", "также", "тоже", "или", "либо", "год", "могут", "будет",
    "стал", "стала", "был", "была", "есть", "может", "очень", "так", "где", "когда",
])


def _is_semantically_duplicate(title: str) -> bool:
    """Check if 3+ significant words from title match a recently posted title.
    
    Uses a TWO-LEVEL check:
    - Level 1: 3+ significant words overlap → DUPLICATE
    - Level 2: 2+ CORE words (brand + model/event) overlap → DUPLICATE
    
    This catches both obvious and subtle duplicates like:
    - "BMW X5 получил новый двигатель" vs "Новый мотор для BMW X5"
    - "Tesla отзывает 10000 машин" vs "Tesla начала отзывную кампанию"
    """
    global _recent_post_keywords

    # Extract significant words from the new title
    words = re.findall(r'[a-zа-яё]{3,}', title.lower())
    significant = [w for w in words if w not in _SEMANTIC_STOP_WORDS]

    if len(significant) < 2:
        return False

    # Extract core words (brands, models, events) for Level 2 check
    core_words = _extract_core_words_from_title(title)

    for recent_words in _recent_post_keywords:
        # Level 1: 3+ significant words overlap
        matches = sum(1 for w in significant if w in recent_words)
        if matches >= 3:
            return True
        
        # Level 2: 2+ core words overlap (brand + model/event)
        if len(core_words) >= 2:
            recent_core = [w for w in recent_words if w in _ALL_CORE_WORDS_SET]
            core_matches = sum(1 for w in core_words if w in recent_core)
            if core_matches >= 2:
                return True

    return False


def _extract_core_words_from_title(title: str) -> list:
    """Extract core identity words (brands, models, key events) from a title."""
    title_lower = title.lower()
    core = []
    
    # Car brands
    _core_brands = [
        "bmw", "mercedes", "audi", "toyota", "honda", "nissan", "hyundai", "kia",
        "ford", "chevrolet", "porsche", "lexus", "volvo", "tesla", "byd", "zeekr",
        "chery", "haval", "geely", "changan", "exeed", "tank", "renault", "peugeot",
        "skoda", "subaru", "suzuki", "mitsubishi", "jaguar", "infiniti", "genesis",
        "ferrari", "lamborghini", "maserati", "bentley", "rolls-royce", "bugatti",
        "mclaren", "lotus", "fiat", "citroen", "mini", "jeep", "rivian", "lucid",
        "polestar", "aston martin", "alfa romeo",
        # Russian aliases
        "бмв", "мерседес", "фольксваген", "тойота", "хёндай", "киа", "порше",
        "шкода", "джили", "чери", "хавал", "тесла",
    ]
    for brand in _core_brands:
        if brand in title_lower:
            core.append(brand)
            break
    
    # Model names
    model_patterns = [
        r'\b([mglxqsec]\d+)\b',
        r'\b(model\s?[s3xy])\b',
        r'\b(\d{3,4}[ix]?)\b',
        r'\b(corolla|camry|civic|accord|mustang|camaro|corvette|prius|rav4|supra)\b',
        r'\b(taycan|macan|cayenne|panamera|wrangler|bronco|defender)\b',
    ]
    for pattern in model_patterns:
        match = re.search(pattern, title_lower)
        if match:
            core.append(match.group(1).replace(" ", "_"))
            break
    
    # Key events
    event_words = [
        "reveal", "launch", "debut", "unveil", "release", "announce",
        "recall", "recalls", "отзыв", "ban", "запрет", "record", "рекорд",
        "crash", "авария", "merger", "слияни", "bankruptcy", "банкрот",
        "redesign", "рестайлинг", "facelift", "update", "обновлен",
        "премьера", "запуск", "дебют", "анонс", "представлен", "выпуск",
        "скандал", "scandal", "проблем", "sold", "продан", "продаж", "цена", "price",
    ]
    for ew in event_words:
        if ew in title_lower:
            core.append(ew)
            break
    
    return core


# Pre-computed set of all core words for fast membership checking
_ALL_CORE_WORDS_SET = set()
for _b in ["bmw", "mercedes", "audi", "toyota", "honda", "nissan", "hyundai", "kia",
           "ford", "chevrolet", "porsche", "lexus", "volvo", "tesla", "byd", "zeekr",
           "chery", "haval", "geely", "changan", "exeed", "tank", "renault", "peugeot",
           "skoda", "subaru", "suzuki", "mitsubishi", "jaguar", "infiniti", "genesis",
           "ferrari", "lamborghini", "maserati", "bentley", "rolls-royce", "bugatti",
           "mclaren", "lotus", "fiat", "citroen", "mini", "jeep", "rivian", "lucid",
           "polestar", "бмв", "мерседес", "тойота", "хёндай", "киа", "порше", "шкода",
           "тесла", "джили", "чери", "хавал",
           "reveal", "launch", "debut", "unveil", "release", "announce",
           "recall", "recalls", "отзыв", "запрет", "record", "рекорд",
           "авария", "слияни", "банкрот", "рестайлинг", "facelift",
           "премьера", "запуск", "дебют", "анонс", "представлен", "выпуск",
           "скандал", "scandal", "проблем", "продаж", "цена",
           "x5", "x3", "x7", "m3", "m5", "q7", "a4", "a6", "e-class",
           "911", "model", "corolla", "camry", "civic", "mustang", "supra",
           "taycan", "macan", "cayenne", "wrangler", "bronco", "defender",
           "prius", "rav4"]:
    _ALL_CORE_WORDS_SET.add(_b)


def _record_post_title(title: str):
    """Record a posted title's significant words for semantic dedup."""
    global _recent_post_keywords
    words = re.findall(r'[a-zа-яё]{3,}', title.lower())
    significant = [w for w in words if w not in _SEMANTIC_STOP_WORDS]
    _recent_post_keywords.append(significant)
    if len(_recent_post_keywords) > _MAX_RECENT_POSTS:
        _recent_post_keywords = _recent_post_keywords[-_MAX_RECENT_POSTS:]


def _clean_post_text(text: str) -> str:
    """Clean post text: remove markdown links, formatting artifacts, SSE garbage,
    AI meta-comments about duplicates, and other content that should not appear in posts."""
    if not text:
        return text

    # Remove markdown links [text](url) → text (Telegram doesn't support them properly)
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'\1', text)

    # Remove bold/italic
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)

    # Remove SSE/streaming artifacts
    text = re.sub(r'data:\s*\{[^}]*\}', '', text)
    text = re.sub(r'\[DONE\]', '', text)

    # Remove AI disclaimers
    for phrase in ["As an AI", "Как AI", "Как искусственный интеллект",
                   "powered by pollinations", "pollinations.ai"]:
        text = re.sub(rf'.*{re.escape(phrase)}.*', '', text, flags=re.IGNORECASE)

    # Remove think tags
    text = re.sub(r'<think\b[^>]*>.*?</think\s*>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'</?think[^>]*>', '', text, flags=re.IGNORECASE)

    # ── Remove AI meta-comments / editorial notes that leaked into post text ──
    # Kept to ~15 essential patterns — removed common Russian words that match valid content
    meta_comment_patterns = [
        # Editorial leakage: "тему в канал не ставим", "не наш формат"
        r'[^\n]*тему\s+в\s+канал\s+не\s+ставим[^\n]*',
        r'[^\n]*в\s+канал\s+не\s+ставим[^\n]*',
        r'[^\n]*не\s+наш\s+формат[^\n]*',
        r'[^\n]*перепишу\s+тему[^\n]*',
        r'[^\n]*напишу\s+готовый\s+пост[^\n]*',
        r'[^\n]*я\s+сразу\s+предложу[^\n]*',
        r'[^\n]*без\s+лишних\s+вопросов[^\n]*',
        r'[^\n]*не\s+соответствует\s+(тематик|формат)[^\n]*',
        r'[^\n]*по\s+вашим\s+(же\s+)?правилам[^\n]*',
        # Duplicate/republish meta-comments
        r'[^\n]*дубликат[^\n]*',
        r'[^\n]*ЭТО\s+УЖЕ\s+ОПУБЛИКОВАНО[^\n]*',
        # English variants
        r'[^\n]*already\s+(posted|published|covered|wrote)[^\n]*',
        r'[^\n]*do\s+not\s+(publish|post)[^\n]*',
        # Generic catch-all for editorial notes in brackets
        r'\[[^\]]*(?:не\s+публиков|не\s+став|не\s+наш|редакц|пропуск)[^\]]*\]',
    ]
    for pattern in meta_comment_patterns:
        text = re.sub(pattern, '', text, flags=re.IGNORECASE)

    # Remove AI/assistant name prefixes that leak into posts
    for prefix in ["Ася:", "Asya:", "Assistant:"]:
        if text.startswith(prefix):
            text = text[len(prefix):].strip()

    # Remove formal "Редакция сообщает" phrases — replace with nothing or informal version
    formal_phrases = [
        ("Редакция сообщает:", ""),
        ("Редакция сообщает —", ""),
        ("Редакция сообщает", ""),
        ("Как стало известно редакции,", ""),
        ("Как стало известно редакции", ""),
        ("По данным нашей редакции,", ""),
        ("По данным нашей редакции", ""),
        ("Редакция @sochiautoparts сообщает:", ""),
        ("Редакция @sochiautoparts сообщает", ""),
    ]
    for phrase, replacement in formal_phrases:
        if phrase in text:
            text = text.replace(phrase, replacement)

    # ── Meta-line removal: remove lines that look like editorial notes ──
    # Reduced to 10 core triggers that NEVER appear in valid automotive posts
    _editorial_trigger_phrases = [
        "не ставим", "не наш формат",
        "перепишу тему", "напишу готовый",
        "я сразу предложу", "без лишних вопросов",
        "не для публикации", "внутренняя заметка",
        "для редакции", "редакционная",
    ]
    lines = text.split('\n')
    cleaned_lines = []
    for line in lines:
        line_lower = line.lower().strip()
        is_editorial = False
        for trigger in _editorial_trigger_phrases:
            if trigger in line_lower:
                is_editorial = True
                break
        # Also catch lines that are purely editorial in brackets [...]
        if re.match(r'^\s*\[.*\]\s*$', line) and any(
            kw in line.lower() for kw in ["не", "редакц", "пропуск", "не наш", "формат"]
        ):
            is_editorial = True
        if not is_editorial:
            cleaned_lines.append(line)
    text = '\n'.join(cleaned_lines)

    # Clean up whitespace
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = text.strip()

    return text


def _validate_post_text(text: str) -> bool:
    """Validate post text before sending to channel.
    
    Checks for:
    - Empty text
    - SSE/API artifacts
    - AI meta-comments about duplicates ("already posted", "do not publish")
    - Blocked topics (politics, war, Putin, etc.)
    - Auto-relevance (must contain automotive keywords)
    - Provider ad artifacts
    - Raw JSON
    - Duplicate indicators that leaked through cleaning
    """
    if not text or not text.strip():
        return False

    text_lower = text.lower()

    # Block SSE artifacts
    sse_patterns = [r'data:\s*\{', r'\[DONE\]', r'"type"\s*:\s*"start"', r'"type"\s*:\s*"error"']
    for pattern in sse_patterns:
        if re.search(pattern, text_lower):
            return False

    # Block API errors
    error_patterns = ["authentication error", "no api key", "model not found",
                      "rate limit", "internal server error", "bad request"]
    for pattern in error_patterns:
        if pattern in text_lower:
            return False

    # Block provider ad artifacts
    ad_patterns = ["pollinations.ai", "powered by pollinations", "keep ai accessible"]
    for pattern in ad_patterns:
        if pattern in text_lower:
            return False

    # Block raw JSON
    if text.strip().startswith(('{', '[', '```', 'data:')):
        return False

    # ── Block AI editorial notes that leaked through cleaning ──
    # Reduced to ~20 truly editorial-specific phrases that NEVER appear in valid automotive posts.
    # Removed common Russian words like "уже", "не стоит", "в канал", "отсеивать" etc.
    duplicate_indicator_phrases = [
        # Editorial leakage — AI discussing whether to publish
        "тему в канал не ставим", "в канал не ставим", "не ставим в канал",
        "не наш формат", "перепишу тему", "напишу готовый пост",
        "я сразу предложу", "без лишних вопросов",
        "не соответствует тематик", "не соответствует формату",
        # Duplicate meta-comments (editorial only — not common words)
        "дубликат", "это повтор",
        "повтор будет заметен", "тему сейчас брать нельзя",
        "второй раз не отправляем", "второй раз не публикуем",
        "с повторами строго",
        # AI refusal/editorial discussion
        "я не буду публиковать", "не буду повторять",
        "пропущу эту новость", "могу сразу предложить",
        # English variants
        "already posted", "already published", "do not publish",
        "duplicate post",
    ]
    for phrase in duplicate_indicator_phrases:
        if phrase in text_lower:
            logger.warning(f"Post BLOCKED (duplicate indicator '{phrase}'): {text[:120]}...")
            return False

    # Block political/war content — LAST CHANCE filter before posting
    # NOTE: Uses compound phrases and trailing spaces to avoid false positives:
    #   - "выборы " (with space) instead of "выбор" which blocked partner posts about tire choices
    #   - Removed "flood", "series", "championship", "election", "exam", "movie",
    #     "drug", "theft", "school" — these are handled in news.py with compound phrases
    blocked_keywords = [
        "путин", "кремль", "госдума", "президент росс", "президент сша",
        "сво ", "специальная военная", "мобилизац", "санкци",
        "военные действ", "вооруженн", "министр оборон",
        "украин", "нато", "nato",
        "навальн", "оппозиц", "протест", "митинг",
        "политик", "депутат", "законопроект", "выборы ", "голосован",
    ]
    # Block boring Russian auto brands — 50 years nothing interesting
    blocked_auto_brands = [
        "автоваз", "лада", "lada", "уаз", "uaz", "камаз", "kamaz",
        "соллерс", "vesta", "granta", "niva", "искра", "iskra",
    ]
    for keyword in blocked_keywords:
        if keyword in text_lower:
            logger.warning(f"Post BLOCKED (keyword '{keyword}'): {text[:80]}...")
            return False
    for keyword in blocked_auto_brands:
        if keyword in text_lower:
            logger.warning(f"Post BLOCKED (boring Russian auto brand '{keyword}'): {text[:80]}...")
            return False

    # ── AUTO-RELEVANCE CHECK — if post has NO auto keywords, BLOCK it ──
    # This is the FINAL GATE: even if all other checks pass, a post about
    # a market fire or celebrity gossip must NEVER reach the channel.
    # NOTE: "рынок" removed as standalone keyword — it matches "пожар на рынке".
    # Use compound forms instead: "авторынок", "рынок авто", "рынок запчастей" etc.
    _auto_required_keywords = [
        # Russian auto keywords
        "авто", "автомобиль", "машина", "мотор", "двигатель", "кузов", "салон",
        "транспорт", "запчас", "ремонт", "сервис", "шин", "колес", "топлив",
        "бензин", "дизел", "электромобиль", "гибрид", "новинка", "модель",
        "бренд", "марка", "продаж", "авторынок", "рынок авто", "рынок запчас", "автосалон", "дилер",
        "тест-драйв", "обзор", "концепт", "прототип", "рестайлинг",
        "коробка", "привод", "подвес", "тормоз", "рулев", "пробег",
        "гонк", "ралли", "формул", "F1", "WRC", "Дакар", "автоспорт",
        "эвакуац", "дтп", "авари", "дорожн", "затор", "пробк",
        "азс", "заправк", "шиномонтаж", "автомойк",
        "эвакуатор", "техпомощ", "техосмотр",
        "логистик", "грузоперевозк", "автоперевозк",
        "автокредит", "автострахов", "каско", "осаго",
        "VIN", "ОСАГО", "КАСКО",
        # Car-specific fire/incident keywords (NOT general "рынок" or "пожар")
        "сгоревш машин", "машин сгорел", "авто сгорел", "сгорел автомобил",
        "поджог автомобил", "поджог машин", "возгоран автомобил", "возгоран машин",
        "перебои поставк запчас", "дефицит запчас", "рынок шин",
        # Car brand names
        "BMW", "Mercedes", "Audi", "Volkswagen", "Toyota", "Honda", "Nissan",
        "Mazda", "Subaru", "Hyundai", "Kia", "Ford", "Chevrolet",
        "Porsche", "Lexus", "Volvo", "Tesla", "BYD", "Zeekr", "Li Auto",
        "NIO", "Chery", "Haval", "Geely", "Changan", "Exeed", "Tank",
        "Renault", "Peugeot", "Skoda", "Mitsubishi", "Suzuki",
        "Jaguar", "Land Rover", "Mini", "Jeep", "Infiniti", "Genesis",
        "Rivian", "Lucid", "Polestar", "Maserati", "Ferrari", "Lamborghini",
        # Russian brand aliases
        "БМВ", "МЕРСЕДЕС", "ФОЛЬКСВАГЕН", "ТОЙОТА", "ХЁНДАЙ", "КИА",
        "ПОРШЕ", "ШКОДА", "ДЖИЛИ", "ЧЕРИ", "ХАВАЛ", "ТЕСЛА",
        # English auto keywords
        "car", "auto", "automobile", "vehicle", "motor", "engine", "drive",
        "SUV", "sedan", "coupe", "crossover", "hatchback", "pickup",
        "EV", "BEV", "PHEV", "ICE", "autonomous", "self-driving",
        "horsepower", "torque", "MPG", "range",
        "racing", "rally", "formula", "Dakar", "motorsport",
        "tire", "wheel", "fuel", "electric", "hybrid",
        "recall", "redesign", "launch", "debut",
        "dealership", "showroom",
    ]
    has_auto_keyword = any(kw.lower() in text_lower for kw in _auto_required_keywords)
    if not has_auto_keyword:
        logger.warning(f"Post BLOCKED (no auto-relevant keywords): {text[:120]}...")
        return False

    return True


def _validate_post_text_partner(text: str) -> bool:
    """Validate partner post text — RELAXED version that skips political keyword checks.
    
    Partner posts are about auto services/parts (Rossko, Autopiter, tire shops, etc.)
    and should NEVER contain political content. But they use words like "выбор" 
    (choosing tires/parts) that trigger the standard political filter.
    This validator only checks for API errors, SSE artifacts, and empty text.
    """
    if not text or not text.strip():
        return False

    text_lower = text.lower()

    # Block SSE artifacts
    sse_patterns = [r'data:\s*\{', r'\[DONE\]', r'"type"\s*:\s*"start"', r'"type"\s*:\s*"error"']
    for pattern in sse_patterns:
        if re.search(pattern, text_lower):
            return False

    # Block API errors
    error_patterns = ["authentication error", "no api key", "model not found",
                      "rate limit", "internal server error", "bad request"]
    for pattern in error_patterns:
        if pattern in text_lower:
            return False

    # Block provider ad artifacts
    ad_patterns = ["pollinations.ai", "powered by pollinations", "keep ai accessible"]
    for pattern in ad_patterns:
        if pattern in text_lower:
            return False

    # Block raw JSON
    if text.strip().startswith(('{', '[', '```', 'data:')):
        return False

    return True


def _ensure_footer(text: str) -> str:
    """Ensure post has proper footer matching @sochiautoparts format."""
    # Remove any existing footer elements to avoid duplicates
    text = re.sub(r'\n*Автор\s+@asiaexp_bot', '', text)
    text = re.sub(r'\n*@sochiautoparts', '', text)
    text = re.sub(r'\n*#sochiautoparts', '', text)
    text = text.rstrip()
    # Add proper footer: Author link + channel mention + hashtag
    text += "\n\nАвтор @asiaexp_bot\n@sochiautoparts\n#sochiautoparts"
    return text


def _enforce_char_limit(text: str, has_media: bool) -> str:
    """Smart character limit enforcement — always preserves footer.
    
    Telegram limits:
    - 1024 chars with media (caption)
    - 4096 chars without media (text-only)
    
    This function:
    1. Separates footer from content
    2. Truncates content if needed
    3. Re-attaches footer (always intact)
    """
    footer = "\n\nАвтор @asiaexp_bot\n@sochiautoparts\n#sochiautoparts"
    char_limit = config.TELEGRAM_CAPTION_LIMIT if has_media else config.TELEGRAM_TEXT_LIMIT
    
    if len(text) <= char_limit:
        return text
    
    # Strip existing footer parts to get pure content
    content = text
    for foot_part in ["\n\nАвтор @asiaexp_bot", "\n@sochiautoparts", "\n#sochiautoparts"]:
        content = content.replace(foot_part, "")
    content = content.rstrip()
    
    # Calculate max content length (leave room for footer)
    max_content = char_limit - len(footer)
    if max_content < 100:
        # Absolute minimum — just footer
        return footer.lstrip('\n')
    
    if len(content) > max_content:
        content = content[:max_content - 3] + "..."
    
    return content + footer


class ChannelManager:
    """Manages posting to the @sochiautoparts channel."""

    def __init__(self):
        self._bot: Optional[Bot] = None
        self._last_post_time: float = 0
        self._last_partner_time: float = 0
        self._last_poll_time: float = 0
        self._poll_count: int = 0
        self._post_model_index: int = 0  # For rotating content models
        self._semantic_loaded: bool = False  # Track if we loaded recent post keywords from DB

    # Content models rotation — each post uses a different model for variety
    # NOTE: openai-fast removed — it always returns empty responses for content generation
    _CONTENT_MODELS_ROTATION = [
        "openai-large", "gpt-5.5", "mistral-4", "deepseek",
        "qwen-large", "deepseek-pro", "deepseek-v4", "minimax-m3",
        "qwen3-coder", "llama-3.3", "nova-2",
    ]

    def set_bot(self, bot: Bot) -> None:
        """Set the bot instance for sending messages."""
        self._bot = bot

    async def load_recent_semantic_data(self) -> None:
        """Load recently posted titles from DB into in-memory semantic dedup.
        
        This ensures that after a restart, the bot knows what was recently posted
        and won't re-post the same topics. Called once at startup.
        """
        if self._semantic_loaded:
            return
        
        try:
            titles = await get_recent_post_titles(hours=72, limit=50)
            for title in titles:
                _record_post_title(title)
            self._semantic_loaded = True
            logger.info(f"Loaded {len(titles)} recent post titles into semantic dedup")
        except Exception as e:
            logger.warning(f"Could not load recent post titles for semantic dedup: {e}")

    async def _add_reaction(self, chat_id, message_id: int) -> None:
        """Add a reaction to a post from the channel account."""
        try:
            emoji = random.choice(POST_REACTIONS)
            await self._bot.set_message_reaction(
                chat_id=chat_id,
                message_id=message_id,
                reaction=[ReactionTypeEmoji(emoji=emoji)],
            )
            logger.info(f"Added reaction {emoji} to message {message_id}")
        except Exception as e:
            logger.debug(f"Could not add reaction: {e}")

    async def _download_news_images(self, image_urls: List[str], max_count: int = 3) -> List[bytes]:
        """Download real images from news source URLs.
        
        Tries each URL, downloads only valid content images.
        Filters out: icons, logos, banners, buttons, social media, tracking pixels,
        and images with abnormal dimensions (too wide/narrow = banners/ads).
        Also filters out very large images (>5MB) which are usually full-page
        screenshots or data URIs masquerading as images.
        Returns list of image data bytes.
        """
        images = []
        if not image_urls:
            return images

        # Max size for news images — filter out huge garbage
        # 2MB limit: large images are usually full-page screenshots,
        # data URIs, or high-res photos that Telegram compresses anyway.
        # Partner images use a separate download method with relaxed limits.
        MAX_IMAGE_SIZE = 2 * 1024 * 1024  # 2MB

        for url in image_urls[:max_count * 3]:  # Try extra URLs in case some fail
            if len(images) >= max_count:
                break

            # Skip obviously non-content image URLs
            if self._is_junk_image_url(url):
                logger.debug(f"Skipping junk image URL: {url[:80]}")
                continue

            try:
                async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                    response = await client.get(url, headers={
                        "User-Agent": "Mozilla/5.0 (compatible; AsyaBot/1.0; +https://t.me/asiaexp_bot)",
                    })
                    if response.status_code != 200:
                        continue

                    content = response.content
                    content_type = response.headers.get("content-type", "")

                    # Validate: must be an image and at least 3KB (lowered for news images)
                    if len(content) < 3000:
                        logger.debug(f"Skipping small image ({len(content)} bytes): {url[:80]}")
                        continue

                    # Skip oversized images — they're usually full-page screenshots or garbage
                    if len(content) > MAX_IMAGE_SIZE:
                        logger.debug(f"Skipping huge image ({len(content)} bytes, max {MAX_IMAGE_SIZE}): {url[:80]}")
                        continue

                    # Skip SVG (vector graphics = logos/icons)
                    if b'<svg' in content[:500] or 'svg' in content_type:
                        continue

                    if not any(ft in content_type for ft in ["image/jpeg", "image/png", "image/webp", "image/gif"]):
                        # Check magic bytes if content-type is missing/wrong
                        if content[:3] == b'\xff\xd8\xff' or content[:4] == b'\x89PNG':
                            pass  # Valid JPEG/PNG
                        elif content[:4] == b'RIFF' and content[8:12] == b'WEBP':
                            pass  # Valid WebP
                        elif content[:6] in (b'GIF87a', b'GIF89a'):
                            pass  # Valid GIF
                        else:
                            continue

                    # Validate image dimensions — skip banners, buttons, tiny thumbnails
                    if not self._is_content_image(content):
                        logger.debug(f"Skipping non-content image: {url[:80]}...")
                        continue

                    images.append(content)
                    logger.info(f"Downloaded news image: {url[:80]}... ({len(content)} bytes)")

            except Exception as e:
                logger.debug(f"Failed to download image {url[:50]}: {e}")
                continue

        logger.info(f"Downloaded {len(images)} real images from news")
        return images

    @staticmethod
    def _is_junk_image_url(url: str) -> bool:
        """Check if an image URL is likely a non-content image (logo, icon, banner, ad, etc.)."""
        url_lower = url.lower()

        # Skip common junk patterns in URL path
        junk_keywords = [
            "icon", "logo", "favicon", "avatar", "badge", "button", "btn",
            "banner", "spinner", "loading", "placeholder", "pixel", "tracker",
            "analytics", "social", "share", "facebook", "twitter", "vk.",
            "telegram", "whatsapp", "instagram", "youtube", "tiktok",
            "ad.", "ads/", "advert", "sponsor", "promo",
            "emoji", "smileys", "captcha", "recaptcha",
            "1x1", "spacer", "blank", "transparent", "dot.", "clear",
            "rss", "feed", "subscribe", "newsletter",
            "watermark", "overlay", "frame", "border",
            # Thumbnail/small image patterns — these are NOT content photos
            "thumb", "small", "preview", "mini", "tiny", "crop",
            "resize", "scaled", "lowres", "low-res",
            "gallery-thumb", "list-thumb", "card-thumb",
        ]
        for kw in junk_keywords:
            if kw in url_lower:
                return True

        # Skip URLs with very small size indicators (e.g., 16x16, 32x32, 48x48)
        import re
        size_pattern = re.compile(r'[/=_x](\d{1,3})x(\d{1,3})[/._]')
        size_match = size_pattern.search(url_lower)
        if size_match:
            w, h = int(size_match.group(1)), int(size_match.group(2))
            if w < 100 or h < 100:
                return True  # Too small = icon/thumbnail

        return False

    @staticmethod
    def _is_content_image(image_data: bytes) -> bool:
        """Validate that image data represents a proper content photo, not a banner/ad/logo.
        
        Checks:
        - Minimum size: 3KB (hard filter before this function)
        - Minimum dimensions: 400x300px (lowered for news images that can be smaller)
        - Maximum aspect ratio: 3:1 (skip wide banners) and 1:3 (skip tall skyscraper ads)
        - Minimum pixel area: 120000px (400*300 — skip small thumbnails even if file is big)
        """
        # Minimum size — nothing under 3KB is a content image
        if len(image_data) < 3000:
            return False

        try:
            from PIL import Image
            import io

            img = Image.open(io.BytesIO(image_data))
            width, height = img.size

            # Skip small images (icons, thumbnails, tiny previews)
            # Lowered from 600x400 to 400x300 — news images can be smaller
            if width < 400 or height < 300:
                return False

            # Skip extremely wide images (banners, ad strips)
            if width / max(height, 1) > 3.0:
                return False

            # Skip extremely tall images (skyscraper ads)
            if height / max(width, 1) > 3.0:
                return False

            # Skip small area images (likely icons/buttons/thumbnails even if > 3KB)
            # 120000 = 400*300 — lowered from 240000 to accept more news images
            if width * height < 120000:
                return False

            return True

        except ImportError:
            # PIL not available — soft check: accept the image anyway for news posts
            # (better to post a slightly unvalidated image than no image at all)
            logger.warning("PIL not available, ACCEPTING image without dimension check (soft validation)")
            return True
        except Exception:
            # Can't read image — accept anyway (soft check)
            logger.debug("Can't read image dimensions, accepting as-is (soft validation)")
            return True

    async def _scrape_article_images(self, article_url: str, max_count: int = 5) -> List[bytes]:
        """Scrape images from a news article page.
        
        Extracts og:image and twitter:image from the article HTML.
        Only uses <img> tags as last resort, with strict filtering.
        Returns list of image data bytes (up to max_count).
        """
        images = []
        try:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                response = await client.get(article_url, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                })
                if response.status_code != 200:
                    return images

                html = response.text
                
                # Extract og:image first (usually the main article image)
                og_images = re.findall(r'<meta[^>]+property=["\x27]og:image["\x27][^>]+content=["\x27]([^"\x27]+)["\x27]', html, re.IGNORECASE)
                og_images += re.findall(r'<meta[^>]+content=["\x27]([^"\x27]+)["\x27][^>]+property=["\x27]og:image["\x27]', html, re.IGNORECASE)
                
                # Extract twitter:image
                tw_images = re.findall(r'<meta[^>]+name=["\x27]twitter:image["\x27][^>]+content=["\x27]([^"\x27]+)["\x27]', html, re.IGNORECASE)
                tw_images += re.findall(r'<meta[^>]+content=["\x27]([^"\x27]+)["\x27][^>]+name=["\x27]twitter:image["\x27]', html, re.IGNORECASE)
                
                # Extract <img> tags with strict filtering — only from article body areas
                # Look for images inside <article>, <main>, or with class containing 'content'/'article'
                article_html = ""
                for pattern in [r'<article[^>]*>(.*?)</article>', r'<main[^>]*>(.*?)</main>', r'<div[^>]+class=["\x27][^"\x27]*(?:content|article|post|entry)[^"\x27]*["\x27][^>]*>(.*?)</div>']:
                    matches = re.findall(pattern, html, re.IGNORECASE | re.DOTALL)
                    for match in matches:
                        article_html += match + "\n"
                
                # If no article body found, skip <img> tags entirely (too risky)
                all_img_urls = []
                if article_html:
                    all_img_urls = re.findall(r'<img[^>]+src=["\x27]([^"\x27]+)["\x27]', article_html, re.IGNORECASE)
                
                # Prioritize: og:image > twitter:image > article body images
                candidate_urls = []
                seen = set()
                for url_list in [og_images, tw_images, all_img_urls]:
                    for url in url_list:
                        if url and url not in seen and len(url) > 30:
                            if url.startswith("//"):
                                url = "https:" + url
                            # Skip obvious junk even before downloading
                            if self._is_junk_image_url(url):
                                continue
                            seen.add(url)
                            candidate_urls.append(url)
                
                # Download the images (max 2 from scraping)
                images = await self._download_news_images(candidate_urls, max_count=max_count)

        except Exception as e:
            logger.debug(f"Article scraping failed for {article_url[:50]}: {e}")

        return images

    async def _generate_post_images(self, news_title: str, count: int = 1) -> List[bytes]:
        """Generate multiple images for a news post using AI (fallback).
        Returns list of image data bytes, up to `count` images.
        """
        images = []
        # Different prompts for variety
        prompts = [
            f"Automotive news illustration: {news_title}. Professional automotive photography, "
            f"front three-quarter view, modern car, vibrant colors, high quality, dramatic lighting, no text.",
            f"Automotive news illustration: {news_title}. Side profile shot, "
            f"studio lighting, sleek design, magazine quality, no text overlay.",
            f"Automotive news illustration: {news_title}. Rear angle view, "
            f"dynamic composition, professional car photography, vivid colors, no text.",
            f"Automotive news illustration: {news_title}. Interior detail shot, "
            f"dashboard and steering wheel, premium feel, cinematic lighting, no text.",
            f"Automotive news illustration: {news_title}. Detail close-up, "
            f"headlight or wheel, dramatic lighting, high contrast, no text.",
        ]
        selected_prompts = prompts[:min(count, len(prompts))]

        for i, prompt in enumerate(selected_prompts):
            try:
                image_data = await ai_router._primary.generate_image(prompt, model="flux")
                if image_data:
                    images.append(image_data)
            except Exception as e:
                logger.error(f"Image generation #{i+1} failed: {e}")

        logger.info(f"Generated {len(images)}/{count} AI images for post")
        return images

    async def _generate_post_image(self, news_title: str) -> Optional[bytes]:
        """Generate a single image for a news post using AI (backward compat)."""
        images = await self._generate_post_images(news_title, count=1)
        return images[0] if images else None

    async def _get_post_images(self, news_item: Dict) -> tuple:
        """Get images for a news post with smart strategy.
        
        Strategy:
        1. Try real images from RSS feed (image_urls field)
        2. Scrape article page for images (og:image, twitter:image, article body)
        3. Web search for images related to the news topic
        4. If still no images, generate AI images as fallback
        
        Returns (image_list: List[bytes], source: str)
        source is 'real', 'scraped', 'search', or 'ai' for logging.
        """
        image_list = []
        source = "none"
        
        # Strategy 1: Use real images from RSS feed
        rss_image_urls = news_item.get("image_urls", [])
        if rss_image_urls:
            try:
                rss_images = await self._download_news_images(
                    rss_image_urls, 
                    max_count=MAX_RSS_IMAGES
                )
                if rss_images:
                    image_list.extend(rss_images)
                    source = "real"
                    logger.info(f"Using {len(rss_images)} real images from RSS for: {news_item.get('title', '')[:50]}")
            except Exception as e:
                logger.warning(f"Failed to download RSS images: {e}")
        
        # Strategy 2: Scrape article page for images (ALWAYS try if URL exists)
        if news_item.get("url") and len(image_list) < MAX_IMAGES_PER_POST:
            try:
                scraped = await self._scrape_article_images(
                    news_item["url"], 
                    max_count=MAX_SCRAPE_IMAGES
                )
                if scraped:
                    # Deduplicate by not adding images we already have
                    for img in scraped:
                        if len(image_list) >= MAX_IMAGES_PER_POST:
                            break
                        image_list.append(img)
                    source = "scraped" if source == "none" else source + "+scraped"
                    logger.info(f"Scraped {len(scraped)} images for: {news_item.get('title', '')[:50]}")
            except Exception as e:
                logger.debug(f"Article scraping skipped: {e}")
        
        # Strategy 3: Web search for images related to the topic
        if len(image_list) < 3 and news_item.get("title"):
            try:
                search_image_urls = await enrich_with_search_images(news_item)
                if search_image_urls:
                    searched = await self._download_news_images(search_image_urls, max_count=MAX_SEARCH_IMAGES)
                    if searched:
                        for img in searched:
                            if len(image_list) >= MAX_IMAGES_PER_POST:
                                break
                            image_list.append(img)
                        source = "search" if source == "none" else source + "+search"
                        logger.info(f"Found {len(searched)} images via web search for: {news_item.get('title', '')[:50]}")
            except Exception as e:
                logger.debug(f"Web search image enrichment skipped: {e}")
        
        # Strategy 4: AI generation as fallback (1 image only — no spam!)
        if not image_list:
            try:
                image_list = await self._generate_post_images(
                    news_item.get("title", ""), count=1
                )
                if image_list:
                    source = "ai"
                    logger.info(f"Generated {len(image_list)} AI images (no real images found)")
            except Exception as e:
                logger.warning(f"AI image generation skipped: {e}")
        
        # HARD LIMIT: never more than MAX_IMAGES_PER_POST (10 max — Telegram limit)
        image_list = image_list[:MAX_IMAGES_PER_POST]
        
        return image_list, source

    async def _download_partner_image(self, image_url: str) -> Optional[bytes]:
        """Download a partner program image (logo/banner).
        
        Uses RELAXED validation for partner images — they're often smaller logos/banners
        (5KB+), which is fine for partner posts. News image validation is stricter.
        Handles SVG images by converting them to PNG using cairosvg.
        """
        if not image_url:
            return None
        try:
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                response = await client.get(image_url)
                if response.status_code != 200:
                    return None
                content = response.content
                content_type = response.headers.get("content-type", "")

                # ── Handle SVG images — convert to PNG for Telegram ──
                is_svg = (
                    "svg" in content_type.lower() or
                    b'<svg' in content[:1000] or
                    image_url.lower().endswith('.svg')
                )
                if is_svg:
                    try:
                        import cairosvg
                        import io
                        # Convert SVG → PNG at 512px width (good for Telegram)
                        png_data = cairosvg.svg2png(bytestring=content, output_width=512)
                        if png_data and len(png_data) > 1000:
                            logger.info(f"Converted partner SVG → PNG ({len(png_data)} bytes): {image_url[:60]}")
                            content = png_data
                            content_type = "image/png"
                        else:
                            logger.debug(f"SVG conversion produced tiny output, skipping")
                            return None
                    except ImportError:
                        logger.debug("cairosvg not available, cannot convert SVG partner image")
                        return None
                    except Exception as e:
                        logger.debug(f"SVG → PNG conversion failed: {e}")
                        return None

                # Relaxed size check for partner images — logos can be 5KB+
                # (But after SVG→PNG conversion, image is larger)
                if len(content) < 2000:
                    logger.debug(f"Skipping tiny partner image ({len(content)} bytes)")
                    return None

                # Must be an image type
                if not any(ft in content_type for ft in ["image/png", "image/jpeg", "image/gif", "image/webp"]):
                    # Check magic bytes
                    if not (content[:3] == b'\xff\xd8\xff' or content[:4] == b'\x89PNG' or
                            (content[:4] == b'RIFF' and content[8:12] == b'WEBP') or
                            content[:6] in (b'GIF87a', b'GIF89a')):
                        logger.debug(f"Skipping partner image: not an image, content-type={content_type}")
                        return None

                # Relaxed dimension check for partner images — logos/banners are OK
                try:
                    from PIL import Image
                    import io
                    img = Image.open(io.BytesIO(content))
                    width, height = img.size
                    # Only skip extremely tiny images (under 50px)
                    if width < 50 or height < 50:
                        logger.debug(f"Partner image too small: {width}x{height}")
                        return None
                except ImportError:
                    # No PIL — accept the image anyway for partner posts
                    logger.debug("PIL not available, accepting partner image without dimension check")
                except Exception:
                    # Can't read — accept anyway for partner posts
                    logger.debug("Can't read partner image dimensions, accepting as-is")

                return content
        except Exception as e:
            logger.debug(f"Could not download partner image: {e}")
        return None

    async def _create_poll_from_news(self, news_title: str, news_summary: str = "") -> Optional[Dict]:
        """Generate a poll question and options based on a news item using AI."""
        try:
            response = await ai_router._primary.chat(
                messages=[
                    {"role": "system", "content": (
                        "Ты создаёшь опросы для автоканала в Telegram. "
                        "На основе новости создай опрос: вопрос и 4 варианта ответа. "
                        "Формат строго JSON: {\"question\": \"...\", \"options\": [\"...\", \"...\", \"...\", \"...\"]}. "
                        "Никакого текста кроме JSON. Вопрос короткий и живой. "
                        "Варианты ответа короткие (до 20 символов)."
                    )},
                    {"role": "user", "content": f"Новость: {news_title}\n{news_summary[:300]}"},
                ],
                model="mistral",
                temperature=0.8,
                max_tokens=200,
            )

            if response.error or not response.text:
                return None

            import json
            text = response.text.strip()
            if "```" in text:
                text = text.split("```")[1].strip()
                if text.startswith("json"):
                    text = text[4:].strip()

            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                data = json.loads(text[start:end])
                question = data.get("question", "")
                options = data.get("options", [])
                if question and len(options) >= 2:
                    return {"question": question, "options": options[:4]}

        except Exception as e:
            logger.error(f"Poll generation error: {e}")

        return None

    async def post_news(self, news_item: Optional[Dict] = None) -> bool:
        """
        Post a news item to the channel.
        If no item specified, picks the best unposted news or searches internet.
        """
        if not self._bot:
            logger.error("Bot not set in ChannelManager — cannot post")
            return False

        logger.info(f"post_news: called with item={'provided' if news_item else 'None (will pick best)'}")

        # Check daily limit
        today_count = await get_today_post_count()
        if today_count >= config.CHANNEL_MAX_POSTS_PER_DAY:
            logger.info(f"Daily post limit reached ({today_count}/{config.CHANNEL_MAX_POSTS_PER_DAY})")
            return False

        # Check hourly limit — max 4 posts per hour (2 posts × 2 cycles)
        hourly_count = await get_hourly_post_count()
        if hourly_count >= config.CHANNEL_MAX_POSTS_PER_HOUR:
            logger.info(f"Hourly post limit reached ({hourly_count}/{config.CHANNEL_MAX_POSTS_PER_HOUR})")
            return False

        # Check minimum interval — allow 60s between posts within same cycle
        # This is shorter than the cycle gap (2-5 min) so both posts in a cycle can go through
        min_interval = 60  # 1 minute minimum between any two posts
        if time.time() - self._last_post_time < min_interval:
            logger.info(f"Post interval too short ({time.time() - self._last_post_time:.0f}s < {min_interval}s)")
            return False

        # Get news item if not provided — use Smart Content Engine!
        if not news_item:
            unposted = await get_unposted_news(limit=25)
            logger.info(f"post_news: {len(unposted)} unposted items in DB")
            # Use content engine to pick the best item (interest scoring + topic dedup)
            news_item = await get_best_news_item(unposted)
            if not news_item:
                # Content engine couldn't find anything fresh — skip this cycle
                logger.info("No fresh topics found (content engine)")
                return False
            logger.info(f"post_news: content engine selected: {news_item.get('title', '')[:60]}")

        # ── DEDUPLICATION LAYER 1: DB-level dedup (title hash, keyword overlap) ──
        if news_item and news_item.get("title"):
            # Check with extended 72h window — same topic should not reappear within 3 days
            if await is_duplicate_post(news_item["title"], hours=72):
                logger.warning(f"DB DUPLICATE blocked: {news_item['title'][:60]}")
                if news_item.get("url"):
                    await mark_news_posted(news_item["url"])
                return False

            # ── DEDUPLICATION LAYER 1.5: Direct channel_posts check ──
            # Check the actual posted content in the channel — this catches cases where
            # AI rephrased the title significantly but it's the same event
            try:
                async with aiosqlite.connect(DB_PATH) as db:
                    cutoff = time.time() - (72 * 3600)  # 72h window
                    async with db.execute(
                        "SELECT content FROM channel_posts WHERE created_at >= ? AND post_type = 'news' ORDER BY created_at DESC LIMIT 30",
                        (cutoff,),
                    ) as cursor:
                        rows = await cursor.fetchall()
                        for row in rows:
                            content = row[0] if row else ""
                            if not content:
                                continue
                            # Extract first line (title) from content
                            first_line = content.split('\n')[0].strip()
                            if not first_line:
                                continue
                            # Check core word overlap with the first line of posted content
                            from bot.database import _extract_core_words
                            posted_core = _extract_core_words(first_line)
                            new_core = _extract_core_words(news_item["title"])
                            if len(posted_core) >= 2 and len(new_core) >= 2:
                                overlap = posted_core & new_core
                                if len(overlap) >= 2:
                                    logger.warning(f"CHANNEL DEDUP blocked (core words match: {overlap}): {news_item['title'][:60]}")
                                    if news_item.get("url"):
                                        await mark_news_posted(news_item["url"])
                                    return False
            except Exception as e:
                logger.debug(f"Channel posts dedup check failed: {e}")

            # ── DEDUPLICATION LAYER 2: In-memory semantic dedup ──
            if _is_semantically_duplicate(news_item["title"]):
                logger.warning(f"SEMANTIC DUPLICATE blocked: {news_item['title'][:60]}")
                if news_item.get("url"):
                    await mark_news_posted(news_item["url"])
                return False

            # ── Channel scanner REMOVED ──
            # Was causing false blocks from GitHub Actions IPs (403/429).
            # DB fingerprint + semantic dedup are sufficient.

        # Generate post content using AI
        source_text = ""
        if news_item.get("summary"):
            source_text = news_item["summary"]

        # ── DATE CONTEXT — Ася знает какой сейчас год! ──
        date_context = get_date_context()

        # ── CHANNEL CONTEXT DISABLED ──
        # Was causing editorial leakage — AI discussed why it can't post about topics
        # instead of just choosing a different one. Topic registry deprioritization
        # handles dedup without triggering editorial discussion.
        channel_context = ""

        extra_instructions = (
            f"{date_context} "
            "ПРАВИЛА ДЛЯ ТЕКСТА ПОСТА:\n"
            "1. Перепиши полностью своими словами — оригинальная авторская заметка, не пересказ.\n"
            "2. Пиши живо и интересно — добавь мнение, эмоцию, вопрос или интригу.\n"
            "3. Объясни почему это важно и что значит для обычного водителя.\n"
            "4. Меняй структуру: начинай с вопроса, факта или эмоции.\n"
            "5. Твой ответ — это ГОТОВЫЙ ТЕКСТ ПОСТА для публикации. ТОЛЬКО текст поста, больше ничего.\n"
            "6. Если новость НЕ про автомобили — верни пустой ответ.\n"
            "7. НЕ ДОБАВЛЯЙ никаких редакционных заметок, пометок, внутренних комментариев, "
            "обсуждений темы, пояснений почему тема подходит или не подходит. "
            "ТОЛЬКО чистый текст поста для читателей канала.\n"
        )
        # Channel context removed — was causing AI to discuss editorial decisions
        # in the post text instead of just picking a different topic.

        # ── Translation & uniquification hint based on language ──
        news_lang = news_item.get("lang", "ru")
        translation_hint = get_translation_uniquification_hint(news_lang)
        if translation_hint:
            extra_instructions += f"\n{translation_hint}\n"

        # ── Editorial aside / joke — adds personality ──
        editorial_aside = get_editorial_aside()
        if editorial_aside:
            extra_instructions += (
                f"\nРЕДАКЦИОННАЯ ШУТКА (вставь её органично в текст, если уместно): "
                f"«{editorial_aside}»\n"
                "Не вставляй насильно — только если это естественно вписывается в текст.\n"
            )

        if news_item.get("lang") and news_item.get("lang") != "ru":
            extra_instructions += (
                "Это новость из зарубежного источника. "
                "Переведи на русский язык и АДАПТИРУЙ для русскоязычной аудитории: "
                "добавь контекст (почему это важно для России/СНГ), переведи цены в рубли если возможно, "
                "используй российские аналоги если уместно (марки, модели, цены). "
                "Сохрани суть и факты, но напиши естественно на русском — "
                "не как машинный перевод, а как живой автожурналист. "
                "Объясни непонятные термины, добавь сравнения с российским рынком. "
            )

        # Get images: real from news source → scraped from article → web search → AI generated
        image_list: List[bytes] = []
        image_source = "none"
        has_media = False
        try:
            image_list, image_source = await self._get_post_images(news_item)

            has_media = len(image_list) > 0
        except Exception as e:
            logger.warning(f"Image retrieval skipped: {e}")

        media_count = len(image_list) if has_media else 0
        
        # Tell AI whether images are real or generated so it can adjust tone
        if image_source in ("real", "scraped", "real+scraped"):
            extra_instructions += (
                "К посту прикреплены РЕАЛЬНЫЕ фотографии из новости. "
                "Не описывай фото — они уже прикреплены. Пиши текст новости. "
                "Если есть реальные фото — обязательно используй. "
            )
        elif has_media:
            extra_instructions += (
                "К посту прикреплены сгенерированные иллюстрации. "
                "Не описывай их подробно — они иллюстративные. "
                "AI-иллюстрации только если фото нет. "
            )

        # ── TONE ANALYSIS — Determine appropriate tone for this news ──
        try:
            from bot.content_engine import analyze_news_tone, get_tone_specific_joke, validate_facts_in_text
            facts = await analyze_news_tone(
                news_item.get("title", ""),
                news_item.get("summary", ""),
                source_text
            )
            logger.info(f"📊 Tone analysis: {facts.tone.value}, brand={facts.brand}, model={facts.model}")
            
            # Add tone-specific joke if appropriate (NOT for serious news)
            if facts.tone.value != "serious" and not news_item.get("is_partner"):
                tone_joke = get_tone_specific_joke(facts.tone)
                if tone_joke and tone_joke not in post_text:
                    post_text = post_text.rstrip() + f"\\n\\n✏️ {tone_joke}"
                    logger.info(f"✅ Added tone joke for {facts.tone.value}")
        except Exception as e:
            logger.warning(f"Tone analysis failed: {e}")
            facts = None
        
        response = await ai_router.generate_channel_post(
            topic=news_item["title"],
            source_text=source_text,
            extra_instructions=extra_instructions,
            has_media=has_media,
            media_count=media_count,
        )

        if response.error or not response.text:
            logger.error(f"Failed to generate channel post: {response.error_message}")
            return False

        post_text = _clean_post_text(response.text)
        post_text = _ensure_footer(post_text)

        # Validate before posting
        if not _validate_post_text(post_text):
            logger.error(f"Post validation failed, skipping")
            return False

        # ── Post-generation dedup REMOVED ──
        # The DB fingerprint check at Layer 1 already covers this.
        # Post-gen checks were redundant and caused false blocks when AI rewrote
        # the same news differently (which is the desired behavior).

        # Smart character limit enforcement — always preserve footer
        post_text = _enforce_char_limit(post_text, has_media)

        # Re-validate after all processing steps
        if not _validate_post_text(post_text):
            logger.error(f"Post validation failed after review, skipping")
            return False

        # ── Channel scanner dedup and post-gen DB fingerprint checks REMOVED ──
        # These were causing false blocks. DB fingerprint at Layer 1 + semantic
        # dedup at Layer 2 are sufficient. Post-gen checks were overly aggressive
        # and blocked valid rewrites of the same topic.

        # ── DB fingerprint dedup — last chance to catch duplicates ──
        # Check both title AND generated content against DB with 72h window
        try:
            if await is_duplicate_post(news_item.get("title", ""), content=post_text, hours=72):
                logger.warning(f"DB dedup blocked post (post-gen): {news_item.get('title', '')[:60]}")
                return False
        except Exception:
            pass  # DB check is best-effort

        # ── Register topic in registry AFTER successful validation ──
        # (not before AI generation — that was causing premature registration)
        entity_key = _extract_entities(news_item.get("title", ""))
        if entity_key:
            _register_topic(entity_key, news_item.get("title", ""))

        # ── Record in in-memory dedup BEFORE publishing ──
        # This ensures the second post in the same cycle will see this one.
        _record_post_title(news_item.get("title", ""))

        # HARD SAFETY CHECK: never post more than MAX_IMAGES_PER_POST images
        if has_media and image_list and len(image_list) > MAX_IMAGES_PER_POST:
            logger.warning(f"SAFETY: Truncating {len(image_list)} images to {MAX_IMAGES_PER_POST}")
            image_list = image_list[:MAX_IMAGES_PER_POST]

        # Post to channel
        try:
            if has_media and image_list:
                # Save images to temp files
                tmp_paths = []
                for i, img_data in enumerate(image_list[:MAX_IMAGES_PER_POST]):
                    tmp_path = os.path.join(tempfile.gettempdir(), f"asya_post_{int(time.time())}_{i}.png")
                    with open(tmp_path, "wb") as f:
                        f.write(img_data)
                    tmp_paths.append(tmp_path)

                if len(tmp_paths) == 1:
                    # Single image — use send_photo
                    photo = FSInputFile(tmp_paths[0], filename="asya_post.png")
                    sent = await self._bot.send_photo(
                        chat_id=config.CHANNEL_ID,
                        photo=photo,
                        caption=post_text[:config.TELEGRAM_CAPTION_LIMIT],
                        parse_mode=ParseMode.HTML,
                        disable_notification=True,
                    )
                else:
                    # Multiple images — use send_media_group (album/carousel up to 10)
                    media_group = []
                    for i, tmp_path in enumerate(tmp_paths):
                        photo_file = FSInputFile(tmp_path, filename=f"asya_post_{i}.png")
                        if i == 0:
                            # First image gets the caption
                            media_group.append(InputMediaPhoto(
                                media=photo_file,
                                caption=post_text[:config.TELEGRAM_CAPTION_LIMIT],
                                parse_mode=ParseMode.HTML,
                            ))
                        else:
                            media_group.append(InputMediaPhoto(media=photo_file))

                    messages = await self._bot.send_media_group(
                        chat_id=config.CHANNEL_ID,
                        media=media_group,
                        disable_notification=True,
                    )
                    sent = messages[0]  # First message in group for DB tracking

                # Clean up temp files
                for tmp_path in tmp_paths:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
            else:
                sent = await self._bot.send_message(
                    chat_id=config.CHANNEL_ID,
                    text=post_text,
                    parse_mode=ParseMode.HTML,
                    disable_notification=True,
                )

            # Record in database
            await add_channel_post(
                content=post_text,
                message_id=sent.message_id,
                post_type="news",
                source_url=news_item.get("url", ""),
            )

            # ── DEDUPLICATION: Store fingerprint to prevent duplicates ──
            await add_post_fingerprint(
                title=news_item.get("title", ""),
                content=post_text,
                post_id=sent.message_id,
            )

            # Mark news as posted
            if news_item.get("url"):
                await mark_news_posted(news_item["url"])

            self._last_post_time = time.time()

            # Add reaction to own post
            await self._add_reaction(config.CHANNEL_ID, sent.message_id)

            # Occasionally create a poll based on the news (every 3rd post)
            self._poll_count += 1
            if self._poll_count % 3 == 0:
                asyncio.create_task(self._post_poll(news_item))

            logger.info(f"Posted news to channel: {news_item['title'][:50]}...")

            # Note: _record_post_title is already called BEFORE publishing
            # (in the dedup section above) to ensure same-cycle dedup works.

            return True

        except Exception as e:
            logger.error(f"Error posting to channel: {e}")
            # Try sending without formatting
            try:
                sent = await self._bot.send_message(
                    chat_id=config.CHANNEL_ID,
                    text=post_text,
                    disable_notification=True,
                )
                await add_channel_post(
                    content=post_text,
                    message_id=sent.message_id,
                    post_type="news",
                    source_url=news_item.get("url", ""),
                )

                # ── DEDUPLICATION: Store fingerprint for fallback post too ──
                await add_post_fingerprint(
                    title=news_item.get("title", ""),
                    content=post_text,
                    post_id=sent.message_id,
                )

                if news_item.get("url"):
                    await mark_news_posted(news_item["url"])
                self._last_post_time = time.time()
                await self._add_reaction(config.CHANNEL_ID, sent.message_id)
                return True
            except Exception as e2:
                logger.error(f"Error posting without formatting: {e2}")
                return False

    async def _search_internet_news(self) -> Optional[Dict]:
        """Search the internet for fresh auto news when RSS feeds are empty.
        Uses both web search and Perplexity AI for better coverage."""
        try:
            now = datetime.now(_MOSCOW_TZ)
            queries = [
                f"автомобильные новости сегодня {now.year}",
                "новости автопрома новинки авто",
                "auto news latest today",
            ]
            query = random.choice(queries)

            # Strategy 1: Web search
            results = await web_search(query, max_results=5)

            if results:
                random.shuffle(results)
                for result in results:
                    if not result.title:
                        continue

                    news_item = {
                        "title": result.title,
                        "url": result.url,  # Unique URL from search result
                        "summary": result.snippet or "",
                        "category": "auto",
                        "lang": "ru" if any(c >= '\u0400' for c in result.title) else "en",
                        "image_urls": [],  # Will be filled by scraping if available
                    }

                    # Check for duplicate BEFORE returning
                    if await is_duplicate_post(result.title, hours=72):
                        logger.info(f"Internet news is duplicate: {result.title[:60]}")
                        continue

                    logger.info(f"Found internet news: {news_item['title'][:50]}")
                    return news_item

            # Strategy 2: Perplexity AI search (web-augmented)
            try:
                response = await ai_router._primary.chat(
                    messages=[
                        {"role": "system", "content": (
                            "Ты поисковик автоновостей. Найди самую свежую и интересную "
                            "автомобильную новость за сегодня. Верни ТОЛЬКО название новости "
                            "и краткое описание (2-3 предложения). Формат:\n"
                            "ЗАГОЛОВОК: ...\nОПИСАНИЕ: ..."
                        )},
                        {"role": "user", "content": f"Найди свежую автоновость ({now.strftime('%d.%m.%Y')})"},
                    ],
                    model="perplexity-fast",
                    temperature=0.5,
                    max_tokens=300,
                )

                if not response.error and response.text:
                    text = response.text.strip()
                    title = ""
                    summary = ""

                    if "ЗАГОЛОВОК:" in text:
                        parts = text.split("ОПИСАНИЕ:")
                        title = parts[0].replace("ЗАГОЛОВОК:", "").strip()
                        summary = parts[1].strip() if len(parts) > 1 else ""
                    else:
                        # Fallback: use first line as title
                        lines = text.split('\n', 1)
                        title = lines[0].strip()
                        summary = lines[1].strip() if len(lines) > 1 else text

                    if title:
                        return_dict = {
                            "title": title,
                            "url": f"https://t.me/sochiautoparts/perplexity/{int(time.time())}",  # Unique URL
                            "summary": summary,
                            "category": "auto",
                            "lang": "ru",
                        }
                        # Check for duplicate before returning
                        if not await is_duplicate_post(title, hours=48):
                            return return_dict
                        else:
                            logger.info(f"Perplexity news is duplicate: {title[:60]}")
            except Exception as e:
                logger.debug(f"Perplexity news search failed: {e}")

            return None

        except Exception as e:
            logger.error(f"Internet news search error: {e}")
            return None

    async def _post_poll(self, news_item: Dict) -> None:
        """Create a poll in the channel based on a news item."""
        try:
            now = time.time()
            if now - self._last_poll_time < 3600:
                return

            poll_data = await self._create_poll_from_news(
                news_item.get("title", ""),
                news_item.get("summary", ""),
            )

            if not poll_data:
                return

            question = poll_data["question"]
            options = poll_data["options"]

            await self._bot.send_poll(
                chat_id=config.CHANNEL_ID,
                question=f"📊 {question}",
                options=options,
                is_anonymous=True,
                disable_notification=True,
            )

            self._last_poll_time = time.time()
            logger.info(f"Posted poll: {question}")

        except Exception as e:
            logger.error(f"Poll posting error: {e}")

    async def post_partner_content(self) -> bool:
        """
        Post a partner/admitad post to the channel with partner image.
        Only posts if within daily limits and interval.
        Deduplicates — won't post the same partner program twice in a row.
        """
        if not self._bot:
            logger.error("Bot not set in ChannelManager")
            return False

        if not partner_manager.should_post_partner():
            return False

        today_count = await get_today_post_count()
        if today_count >= config.CHANNEL_MAX_POSTS_PER_DAY:
            return False

        # Get the last posted partner program name to avoid duplicates
        last_partner_name = getattr(self, '_last_partner_name', '')

        # Try to get a DIFFERENT program than the last one
        program = partner_manager.get_random_program()
        if not program:
            logger.info("No partner programs available")
            return False

        # If same as last, try a few more times to get a different one
        if last_partner_name and program.name == last_partner_name:
            for _ in range(5):
                alt = partner_manager.get_random_program()
                if alt and alt.name != last_partner_name:
                    program = alt
                    break

        # Check if this partner program was already posted recently (dedup)
        if await is_duplicate_post(f"Партнёр: {program.name}", hours=12):
            logger.info(f"Partner program '{program.name}' was already posted recently, skipping")
            return False

        # Try to use partner image from the PartnerProgram object directly
        # (use program.image URL — don't search by category/name matching)
        partner_image_url = program.image
        partner_image_data = None
        if partner_image_url:
            try:
                partner_image_data = await self._download_partner_image(partner_image_url)
                if partner_image_data:
                    logger.info(f"Using partner image from program object: {partner_image_url[:60]}")
            except Exception as e:
                logger.debug(f"Partner image download failed: {e}")

        if not partner_image_data:
            try:
                partner_image_data = await self._generate_post_image(f"{program.name} automotive service")
            except Exception:
                pass

        has_media = partner_image_data is not None

        post_content = await partner_manager.generate_partner_post_content(program)

        response = await ai_router.generate_channel_post(
            topic=f"Рекомендация сервиса: {program.name}",
            source_text=f"Партнёрская программа: {program.name}. Описание: {program.description or 'Автомобильный сервис'}",
            extra_instructions=(
                "Это партнёрский пост — рекомендация сервиса для автомобилистов. "
                "Напиши естественно, как живая девушка-автоэксперт, которая советует проверенный сервис. "
                "Не делай это откровенной рекламой — вставь рекомендацию органично. "
                f"Ссылка: {program.format_link()}"
            ),
            has_media=has_media,
            media_count=1,
        )

        if not response.error and response.text:
            post_content = response.text

        post_content = _clean_post_text(post_content)
        post_content = _ensure_footer(post_content)

        # Validate — partner posts use relaxed validation (skip political keyword check)
        # Partner posts are about auto services/parts, they don't contain political content
        if not _validate_post_text_partner(post_content):
            return False

        # Smart character limit — always preserve footer
        post_content = _enforce_char_limit(post_content, has_media)

        try:
            if has_media and partner_image_data:
                ext = ".png"
                if partner_image_url and ".jpg" in partner_image_url.lower():
                    ext = ".jpg"

                tmp_path = os.path.join(tempfile.gettempdir(), f"asya_partner_{int(time.time())}{ext}")
                with open(tmp_path, "wb") as f:
                    f.write(partner_image_data)

                photo = FSInputFile(tmp_path, filename=f"partner_{program.name[:20]}.{ext.lstrip('.')}")
                sent = await self._bot.send_photo(
                    chat_id=config.CHANNEL_ID,
                    photo=photo,
                    caption=post_content[:config.TELEGRAM_CAPTION_LIMIT],
                    parse_mode=ParseMode.HTML,
                    disable_notification=True,
                )

                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            else:
                sent = await self._bot.send_message(
                    chat_id=config.CHANNEL_ID,
                    text=post_content,
                    parse_mode=ParseMode.HTML,
                    disable_notification=True,
                )

            await add_partner_post(
                program_id=program.id,
                program_name=program.name,
                category=program.category or "general",
                affiliate_url=program.goto_link,
                post_content=post_content,
                message_id=sent.message_id,
            )

            await add_channel_post(
                content=post_content,
                message_id=sent.message_id,
                post_type="partner",
                partner_program=program.name,
            )

            partner_manager.mark_posted()
            self._last_partner_time = time.time()
            self._last_partner_name = program.name  # Track last partner for dedup

            # Store fingerprint for partner dedup
            await add_post_fingerprint(
                title=f"Партнёр: {program.name}",
                content=post_content,
                post_id=sent.message_id,
            )

            await self._add_reaction(config.CHANNEL_ID, sent.message_id)

            logger.info(f"Posted partner content: {program.name}")
            return True

        except Exception as e:
            logger.error(f"Error posting partner content: {e}")
            try:
                sent = await self._bot.send_message(
                    chat_id=config.CHANNEL_ID,
                    text=post_content,
                    disable_notification=True,
                )
                await add_partner_post(
                    program_id=program.id,
                    program_name=program.name,
                    category=program.category or "general",
                    affiliate_url=program.goto_link,
                    post_content=post_content,
                    message_id=sent.message_id,
                )
                await add_channel_post(
                    content=post_content,
                    message_id=sent.message_id,
                    post_type="partner",
                    partner_program=program.name,
                )
                partner_manager.mark_posted()
                self._last_partner_time = time.time()
                await self._add_reaction(config.CHANNEL_ID, sent.message_id)
                return True
            except Exception as e2:
                logger.error(f"Error posting partner content without formatting: {e2}")
                return False

    async def run_scheduled_post(self) -> bool:
        """
        Run a scheduled post — tries up to 3 different news items per cycle.
        
        SIMPLIFIED PIPELINE:
        1. Try partner content (20% chance if interval met)
        2. Try news — up to 3 attempts with DIFFERENT items
        3. Fallback: AI-generated "fun fact" if no web news works
        
        Each cycle is called TWICE per 30-min interval (2 different posts).
        Dedup ensures the 2nd post is always different from the 1st.
        """
        logger.info("run_scheduled_post: called")
        now = time.time()
        partner_interval = config.PARTNER_POST_INTERVAL_HOURS * 3600

        # Try partner content with 20% probability (if interval met)
        # Lower probability so news posts dominate — partner posts are supplementary
        if (now - self._last_partner_time >= partner_interval and
                partner_manager.should_post_partner()):
            if random.random() < 0.2:
                result = await self.post_partner_content()
                if result:
                    return True
                # Partner post failed/skipped — fall through to news
        
        # Primary: post NEWS — try up to 3 DIFFERENT items per cycle
        for attempt in range(3):
            result = await self.post_news()
            if result:
                return True
            logger.info(f"run_scheduled_post: attempt {attempt + 1}/3 failed, trying different item")
            # Brief pause between attempts to avoid hammering AI
            if attempt < 2:
                await asyncio.sleep(2)

        # Fallback: generate a "fun fact" / "did you know" style post
        logger.info("All news attempts failed — trying fallback fun fact post")
        result = await self._post_fallback_fun_fact()
        if result:
            return True

        # Final fallback: try partner content
        if partner_manager.should_post_partner():
            return await self.post_partner_content()

        return False

    async def _post_fallback_fun_fact(self) -> bool:
        """Generate a 'fun fact' / 'did you know' style post when no web news is available.
        
        This ensures the channel always has content even when web search fails
        or all results are dedup-filtered.
        """
        if not self._bot:
            return False

        # Check daily/hourly limits
        today_count = await get_today_post_count()
        if today_count >= config.CHANNEL_MAX_POSTS_PER_DAY:
            return False
        hourly_count = await get_hourly_post_count()
        if hourly_count >= config.CHANNEL_MAX_POSTS_PER_HOUR:
            return False

        # Pick a diverse automotive topic category
        _FUN_FACT_CATEGORIES = [
            "автомобильные рекорды Гиннесса",
            "самые дорогие автомобили мира",
            "интересные факты об автомобилях",
            "история автомобильных брендов",
            "автомобильные мифы и легенды",
            "необычные автомобили мира",
            "самые быстрые автомобили в истории",
            "автомобильные изобретения которые изменили мир",
            "интересные случаи на дорогах",
            "реставрация винтажных автомобилей",
            "автомобильная мода и тренды",
            "как выбирают автомобили эксперты",
            "автомобильные мошенничества схемы",
            "секреты тюнинга от профессионалов",
            "автоспорт интересные факты Формула 1",
        ]
        category = random.choice(_FUN_FACT_CATEGORIES)
        now = datetime.now(_MOSCOW_TZ)
        month_ru = ["января", "февраля", "марта", "апреля", "мая", "июня",
                   "июля", "августа", "сентября", "октября", "ноября", "декабря"]
        date_str = f"{now.day} {month_ru[now.month - 1]} {now.year}"

        extra_instructions = (
            f"Сегодня {date_str}. "
            f"Напиши интересный короткий пост на тему: {category}. "
            "Это НЕ новость — это познавательный пост 'А вы знали?' для автоканала. "
            "Пиши живо, с эмоцией и интригой. Добавь конкретные цифры и факты. "
            "Твой ответ — ТОЛЬКО готовый текст поста для публикации. "
            "Никаких пояснений, примечаний, редакционных заметок. "
            "Если не можешь написать — верни пустой ответ. "
        )

        response = await ai_router.generate_channel_post(
            topic=category,
            source_text="",
            extra_instructions=extra_instructions,
            has_media=False,
            media_count=0,
        )

        if response.error or not response.text:
            logger.warning(f"Fallback fun fact generation failed: {response.error_message}")
            return False

        post_text = _clean_post_text(response.text)
        post_text = _ensure_footer(post_text)

        if not _validate_post_text(post_text):
            logger.warning("Fallback fun fact validation failed")
            return False

        post_text = _enforce_char_limit(post_text, has_media=False)

        try:
            sent = await self._bot.send_message(
                chat_id=config.CHANNEL_ID,
                text=post_text,
                parse_mode=ParseMode.HTML,
                disable_notification=True,
            )

            await add_channel_post(
                content=post_text,
                message_id=sent.message_id,
                post_type="fun_fact",
                source_url="",
            )

            await add_post_fingerprint(
                title=f"fun_fact: {category}",
                content=post_text,
                post_id=sent.message_id,
            )

            self._last_post_time = time.time()
            await self._add_reaction(config.CHANNEL_ID, sent.message_id)

            logger.info(f"Posted fallback fun fact: {category}")
            return True

        except Exception as e:
            logger.error(f"Error posting fallback fun fact: {e}")
            return False


# ── Global instance ────────────────────────────────────────────────────────────

channel_manager = ChannelManager()
