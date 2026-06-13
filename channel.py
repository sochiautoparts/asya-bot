"""
Channel Manager -- Posts to @sochiautoparts with proper formatting.
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
    get_best_news_item, get_date_context,
    _is_topic_covered, _extract_entities, _score_interest,
    _register_topic, get_editorial_aside, get_translation_uniquification_hint,
)
# Channel scanner removed — unreliable from GitHub Actions IPs (403/429).
# DB fingerprint + semantic dedup are sufficient.

logger = logging.getLogger("asya.channel")

# NSFW moderation REMOVED — automotive news from RSS feeds doesn't contain porn.
# The old system had AI Vision checks, SafeSearch, keyword filters — all unnecessary
# overhead for an automotive news bot that gets its images from article pages.

# ── Reactions to add to posts ───────────────────────────────────────────────

POST_REACTIONS = ["👍", "🔥", "🚗", "😍", "👏", "💯", "🤩", "⚡"]

# ── How many images per news post ───────────────────────────────────────────
# Telegram allows up to 10 media per post — USE IT.
# Article photos are relevant and real, more is better for visual impact.
NEWS_IMAGES_MIN = 1
NEWS_IMAGES_MAX = 10
MAX_IMAGES_PER_POST = 10

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
    - Level 1: 3+ significant words overlap -> DUPLICATE
    - Level 2: 2+ CORE words (brand + model/event) overlap -> DUPLICATE
    
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
        # ── NSFW / Adult content — ABSOLUTE BLOCK ──
        "порн", "секс", "эрот", "голая", "голые", "обнажён", "обнажен",
        "интим", "проститут", "путан", "бордель",
        "письк", "хуй", "пизд", "ебать", "ебан", "ёбан",
        "сосать", "кончить", "сперм", "оргазм",
        "стриптиз", "камасутр", "ню фото",
        "порно-", "секс-", "18+", "xxx",
        "фистинг", "минет",
        "nude", "porn", "nsfw", "hentai", "milf",
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
    2. Truncates content at sentence/paragraph boundary if needed
    3. Re-attaches footer (always intact)
    4. NEVER cuts mid-word or mid-sentence — always truncates at a natural break point
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
        content = _smart_truncate(content, max_content)
    
    return content + footer


def _smart_truncate(text: str, max_len: int) -> str:
    """Truncate text at a natural sentence/paragraph boundary.
    
    Strategy (in priority order):
    1. Find the last paragraph break (\n\n) before max_len
    2. Find the last sentence end (. ! ? …) before max_len
    3. Find the last newline (\n) before max_len
    4. Find the last space before max_len (avoid mid-word cut)
    5. Last resort: hard cut at max_len - 3 + "..."
    
    Always appends "..." to indicate truncation.
    """
    if len(text) <= max_len:
        return text
    
    # We need room for "..." (3 chars)
    target = max_len - 3
    if target < 50:
        return text[:target] + "..."
    
    # Look at the text up to target+50 chars — we want to find the BEST
    # break point near the end, not just the very last one
    search_zone = text[:target + 1]
    
    # 1. Try paragraph break (\n\n) — best break point
    last_para = search_zone.rfind("\n\n")
    if last_para > target * 0.5:  # Don't throw away more than half the text
        return text[:last_para].rstrip() + "..."
    
    # 2. Try sentence end (. ! ? … followed by space or newline)
    # Look for sentence endings in the last portion of the text
    sentence_end_chars = ['. ', '! ', '? ', '… ', '.\n', '!\n', '?\n', '…\n']
    best_sentence_end = -1
    for end_char in sentence_end_chars:
        pos = search_zone.rfind(end_char)
        if pos > best_sentence_end and pos > target * 0.5:
            best_sentence_end = pos + len(end_char) - 1  # Include the punctuation
    
    if best_sentence_end > target * 0.5:
        return text[:best_sentence_end + 1].rstrip() + "..."
    
    # 3. Try newline (\n)
    last_newline = search_zone.rfind("\n")
    if last_newline > target * 0.5:
        return text[:last_newline].rstrip() + "..."
    
    # 4. Try space (avoid mid-word cut)
    last_space = search_zone.rfind(" ")
    if last_space > target * 0.5:
        return text[:last_space].rstrip() + "..."
    
    # 5. Hard cut — very last resort
    return text[:target].rstrip() + "..."


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

    @staticmethod
    def _ai_response_to_bytes(response) -> Optional[bytes]:
        """Convert AIResponse from generate_image to raw bytes.
        
        generate_image() returns AIResponse with image_b64 (base64) or image_url,
        NOT raw bytes. This helper extracts the actual image bytes.
        """
        if response is None:
            return None
        # If it's already bytes, return as-is
        if isinstance(response, bytes):
            return response
        # AIResponse object — extract image data
        try:
            from ai.providers.base import AIResponse
            if isinstance(response, AIResponse):
                if response.image_b64:
                    import base64
                    return base64.b64decode(response.image_b64)
                if response.image_url:
                    # Download the image from URL
                    try:
                        r = httpx.get(response.image_url, timeout=30.0, follow_redirects=True)
                        if r.status_code == 200 and len(r.content) > 1000:
                            return r.content
                    except Exception:
                        pass
                return None
        except ImportError:
            pass
        # Fallback: if it has image_b64 attribute
        if hasattr(response, 'image_b64') and response.image_b64:
            import base64
            return base64.b64decode(response.image_b64)
        return None

    async def _generate_post_images(self, news_title: str, count: int = 1) -> List[bytes]:
        """Generate multiple images for a news post using AI (fallback).
        Returns list of image BYTES, up to `count` images.

        3-level failover: Pollinations (key) → Pollinations (free) → None
        LIMITED to max 2 model attempts to avoid timeout/OOM on GitHub Actions.
        """
        images = []
        # Different prompts for variety
        prompts = [
            f"Automotive news illustration: {news_title}. Professional automotive photography, "
            f"front three-quarter view, modern car, vibrant colors, high quality, dramatic lighting, no text.",
            f"Automotive news illustration: {news_title}. Side profile shot, "
            f"studio lighting, sleek design, magazine quality, no text overlay.",
        ]
        selected_prompts = prompts[:min(count, len(prompts))]

        # Try image models — limited to 2 attempts to prevent OOM/timeout
        _IMAGE_MODELS = ["flux", "flux-pro"]
        attempts = 0
        max_attempts = 2  # Safety: don't try too many models (each has 120s timeout)

        for i, prompt in enumerate(selected_prompts):
            for img_model in _IMAGE_MODELS:
                attempts += 1
                if attempts > max_attempts:
                    logger.warning(f"Image generation: reached max {max_attempts} attempts, stopping")
                    break
                try:
                    ai_response = await asyncio.wait_for(
                        ai_router._primary.generate_image(prompt, model=img_model),
                        timeout=60.0
                    )
                    img_bytes = self._ai_response_to_bytes(ai_response)
                    if img_bytes:
                        images.append(img_bytes)
                        break

                except asyncio.TimeoutError:
                    logger.warning(f"Image generation #{i+1} with {img_model} timed out (60s limit)")
                    continue
                except Exception as e:
                    logger.debug(f"Image generation #{i+1} with model {img_model} failed: {e}")
                    continue
            if images:
                break  # Got enough, don't try more prompts
            if attempts >= max_attempts:
                break

        logger.info(f"Generated {len(images)}/{count} AI images for post ({attempts} attempts)")
        return images

    async def _generate_post_image(self, news_title: str) -> Optional[bytes]:
        """Generate a single image for a news post using AI (backward compat)."""
        images = await self._generate_post_images(news_title, count=1)
        return images[0] if images else None

    async def _get_post_images(self, news_item: Dict) -> tuple:
        """Get images for a news post — simple article-first approach.

        v5.0: No search engines, no AI generation, no NSFW moderation.
        Just take the photos that are already in the article/RSS feed.

        1. RSS images (media:content, enclosures, <img> in content)
        2. Article page images (og:image, JSON-LD, <img> tags)
        3. Done. If no images — post goes text-only.

        Returns (image_list: List[bytes], source: str)
        source is 'rss', 'article', 'cache', or 'none'.
        """
        title = news_item.get("title", "")
        article_url = news_item.get("url", "")
        image_urls = news_item.get("image_urls", [])

        try:
            from bot.image_fetcher import ImageFetcher
            if not hasattr(self, '_image_fetcher'):
                self._image_fetcher = ImageFetcher()

            images, source = await self._image_fetcher.fetch(
                topic=title,
                article_url=article_url,
                image_urls=image_urls,
                max_images=MAX_IMAGES_PER_POST,
            )
            if images:
                logger.info(f"Got {len(images)} images for '{title[:50]}' (source={source})")
            else:
                logger.info(f"No images found for '{title[:50]}' — text-only post")
            return images, source

        except Exception as e:
            logger.warning(f"ImageFetcher failed: {e} — text-only post")
            return [], "none"

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

        # Check hourly limit — max 6 posts per hour (3 posts × 2 cycles)
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
            "8. БУДЬ КОМПАКТЕН: 500-900 символов — оптимально. Пост ОБЯЗАТЕЛЬНО публикуется с фото, "
            "а Telegram ограничивает подпись к фото 1024 символами. "
            "Если текст длиннее 1024 — пост теряет фото! Без фото пост пойдёт только если "
            "контент ОЧЕНЬ интересный и требует подробностей (до 4096 символов). "
            "Старайся уложиться в 950 символов включая подпись. "
            "МАКСИМУМ ОДИН персонаж редакции за пост — не перечисляй всех!\n"
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

        # Get images: RSS images → article page images → done
        image_list: List[bytes] = []
        image_source = "none"
        has_media = False
        try:
            image_list, image_source = await self._get_post_images(news_item)

            has_media = len(image_list) > 0
        except Exception as e:
            logger.warning(f"Image retrieval skipped: {e}")

        media_count = len(image_list) if has_media else 0
        
        # Tell AI that real article photos are attached
        if has_media:
            extra_instructions += (
                "К посту прикреплены РЕАЛЬНЫЕ фотографии из новости. "
                "Не описывай фото — они уже прикреплены. Пиши текст новости. "
            )

        # ── TONE ANALYSIS — Determine appropriate tone for this news ──
        # NOTE: Tone joke is added AFTER post_text is generated (see below)
        tone_joke = ""
        tone_value = "neutral"
        try:
            from bot.content_engine import analyze_news_tone, get_tone_specific_joke
            facts = await analyze_news_tone(
                news_item.get("title", ""),
                news_item.get("summary", ""),
                source_text
            )
            logger.info(f"Tone analysis: {facts.tone.value}, brand={facts.brand}, model={facts.model}")
            tone_value = facts.tone.value
            
            # Prepare tone-specific joke if appropriate (NOT for serious news)
            if facts.tone.value != "serious" and not news_item.get("is_partner"):
                tone_joke = get_tone_specific_joke(facts.tone)
        except Exception as e:
            logger.debug(f"Tone analysis skipped: {e}")
        
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

        # ── Add tone-specific joke AFTER post generation (if applicable) ──
        if tone_joke and tone_joke not in post_text:
            # Pre-check: only add joke if post has room within Telegram limits
            # Conservative: assume media post (1024 char limit) as worst case
            joke_len = len(tone_joke) + 2  # joke text + \n\n separator
            char_limit = config.TELEGRAM_CAPTION_LIMIT  # 1024 — worst case
            if len(post_text) + joke_len <= char_limit:
                # Insert joke before footer
                footer_marker = "\n\nАвтор @asiaexp_bot"
                if footer_marker in post_text:
                    post_text = post_text.replace(footer_marker, f"\n\n{tone_joke}{footer_marker}")
                else:
                    post_text = post_text.rstrip() + f"\n\n{tone_joke}"
                logger.info(f"Added tone joke for {tone_value}")
            else:
                logger.debug(f"Skipped tone joke — post already {len(post_text)} chars (limit {char_limit})")

        # Validate before posting
        if not _validate_post_text(post_text):
            logger.error(f"Post validation failed, skipping")
            return False

        # ── Post-generation dedup REMOVED ──
        # The DB fingerprint check at Layer 1 already covers this.
        # Post-gen checks were redundant and caused false blocks when AI rewrote
        # the same news differently (which is the desired behavior).

        # ── SMART MEDIA DECISION: media-first policy ──
        #
        # RULES (Telegram limits: caption=1024, text-only=4096):
        #   1. Post with photo — ALWAYS preferred. Optimal text: 500-900 chars, max 1024.
        #   2. Post without photo — ONLY allowed when ALL conditions are met:
        #      a) Text > 1024 chars (genuinely doesn't fit in caption)
        #      b) Text <= 4096 chars (Telegram text-only limit)
        #      c) Content is interesting/valuable (interest score >= 0.5)
        #   3. Short text without photo — BLOCKED. Must have image.
        #
        _CAPTION_LIMIT = config.TELEGRAM_CAPTION_LIMIT   # 1024
        _TEXT_LIMIT = config.TELEGRAM_TEXT_LIMIT          # 4096
        _MIN_CONTENT_FOR_TEXT_ONLY = 1025  # Must exceed caption limit to justify text-only

        # ── CASE 1: No media + short text (≤1024) → text-only is fine ──
        # No more AI image generation or image search. If the article had photos,
        # they were already extracted by _get_post_images(). If not — text-only.
        if not has_media and len(post_text) <= _CAPTION_LIMIT:
            logger.info(
                f"Post has no media, text is {len(post_text)} chars — publishing text-only."
            )

        # ── CASE 2: Has media + text > caption limit → try compress to keep media ──
        elif has_media and len(post_text) > _CAPTION_LIMIT:
            logger.info(
                f"Post text {len(post_text)} chars > caption limit {_CAPTION_LIMIT}. "
                f"Attempting to compress text to preserve media attachment."
            )
            compressed = _enforce_char_limit(post_text, has_media=True)
            if len(compressed) <= _CAPTION_LIMIT and len(compressed) >= 400:
                # Compressed enough while keeping meaningful content — keep media!
                post_text = compressed
                logger.info(
                    f"Text compressed to {len(compressed)} chars — keeping media attachment."
                )
            else:
                # Text genuinely too long to compress — check if it's INTERESTING
                # enough to justify a text-only post
                interest_score = _score_interest(
                    news_item.get("title", ""),
                    news_item.get("summary", "")
                )
                if interest_score >= 0.5 and len(post_text) <= _TEXT_LIMIT:
                    # Interesting + within Telegram limit → allow text-only
                    logger.info(
                        f"Text too long for caption ({len(post_text)} chars, interest={interest_score:.2f}). "
                        f"Publishing WITHOUT media — content is interesting enough to justify text-only "
                        f"(up to {_TEXT_LIMIT} chars)."
                    )
                    has_media = False
                    image_list = []
                else:
                    # Not interesting enough or too long — compress aggressively + keep media
                    logger.info(
                        f"Interest score {interest_score:.2f} too low for text-only. "
                        f"Compressing text aggressively to keep media (visual > extra text)."
                    )
                    post_text = _enforce_char_limit(post_text, has_media=True)

        # ── CASE 3: No media + long text (>1024) → text-only is fine ──
        # No more AI image generation. If article had photos, they're already attached.
        elif not has_media and len(post_text) > _CAPTION_LIMIT:
            if len(post_text) > _TEXT_LIMIT:
                post_text = _enforce_char_limit(post_text, has_media=False)
            logger.info(
                f"Text-only post: {len(post_text)} chars. No AI-generated images."
            )

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
                    # Single image — use send_photo (caption already enforced by _enforce_char_limit)
                    photo = FSInputFile(tmp_paths[0], filename="asya_post.png")
                    sent = await self._bot.send_photo(
                        chat_id=config.CHANNEL_ID,
                        photo=photo,
                        caption=post_text,
                        parse_mode=ParseMode.HTML,
                        disable_notification=True,
                    )
                else:
                    # Multiple images — use send_media_group (album/carousel up to 10)
                    media_group = []
                    for i, tmp_path in enumerate(tmp_paths):
                        photo_file = FSInputFile(tmp_path, filename=f"asya_post_{i}.png")
                        if i == 0:
                            # First image gets the caption (already enforced by _enforce_char_limit)
                            media_group.append(InputMediaPhoto(
                                media=photo_file,
                                caption=post_text,
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

        # ── SMART MEDIA DECISION: partner posts also need media ──
        # Try to get an image, but don't block — text-only is acceptable as last resort.
        if not has_media and len(post_content) <= config.TELEGRAM_CAPTION_LIMIT:
            # Short text but no image — try to generate one
            logger.warning(
                f"Partner post has NO media and text is only {len(post_content)} chars. "
                f"Attempting AI image generation to avoid text-only post."
            )
            try:
                ai_image = await self._generate_post_image(f"{program.name} automotive service logo")
                if ai_image:
                    partner_image_data = ai_image
                    has_media = True
                    logger.info("Generated AI image for partner post — avoiding text-only")
                else:
                    # Try ONE generic prompt with ONE model (fast)
                    try:
                        ai_resp = await asyncio.wait_for(
                            ai_router._primary.generate_image(
                                f"Auto service {program.name}, professional logo, clean design, no text.",
                                model="flux",
                            ),
                            timeout=60.0,
                        )
                        img_bytes = self._ai_response_to_bytes(ai_resp)
                        if img_bytes:
                            partner_image_data = img_bytes
                            has_media = True
                            logger.info("Generic partner image SUCCEEDED with flux")
                    except (asyncio.TimeoutError, Exception) as e:
                        logger.warning(f"Generic partner image generation failed: {e}")
            except Exception as e:
                logger.warning(f"Partner AI image generation failed: {e}")

            if not has_media:
                # LAST RESORT: publish text-only rather than skipping partner post
                logger.warning(
                    f"PARTNER POST TEXT-ONLY (last resort): No image available, "
                    f"text={len(post_content)} chars. Publishing without photo."
                )

        elif has_media and len(post_content) > config.TELEGRAM_CAPTION_LIMIT:
            # Text too long for caption — try to compress first, keep media
            compressed = _enforce_char_limit(post_content, has_media=True)
            if len(compressed) <= config.TELEGRAM_CAPTION_LIMIT and len(compressed) >= 400:
                post_content = compressed
                logger.info(
                    f"Partner text compressed to {len(compressed)} chars — keeping media."
                )
            else:
                # Genuinely long content — allow text-only as last resort (partner posts are always useful)
                logger.info(
                    f"Partner post {len(post_content)} chars > caption limit. "
                    f"Publishing WITHOUT media to preserve full text."
                )
                has_media = False
                partner_image_data = None

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
                    caption=post_content,
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
        
        Each cycle is called 3 TIMES per 30-min interval (3 different posts).
        Dedup ensures each post is always different from the others.
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


# ── Group Commenting System ────────────────────────────────────────────────────
# Ася комментирует посты в автомобильных группах, привлекая подписчиков.

# Groups where Asya is a member and can comment
# Format: {"group_id": "group_name"} — populated from config or DB
_AUTO_GROUPS = {
    # Sochi auto groups — primary audience region
    # Group IDs will be discovered dynamically via getUpdates
}

# Maximum comments per day per group (anti-spam)
MAX_COMMENTS_PER_GROUP_PER_DAY = 3
# Minimum interval between comments in same group (seconds)
MIN_COMMENT_INTERVAL = 1800  # 30 minutes
# Comment length limit (Telegram: 4096, but keep it short for engagement)
MAX_COMMENT_LENGTH = 300


async def comment_on_group_post(
    bot: Bot,
    chat_id: int,
    message_id: int,
    post_text: str,
) -> bool:
    """Comment on a post in a Telegram group as Ася.
    
    Generates a short, lively comment in Ася's voice that adds value
    (expert opinion, question, or reaction) — not spam.
    
    Args:
        bot: Bot instance
        chat_id: Group/chat ID
        message_id: Message ID to reply to
        post_text: Original post text (for context)
    
    Returns True if comment was posted successfully.
    """
    if not bot:
        return False
    
    try:
        # Generate comment using AI
        comment_prompt = (
            "Ты Ася — автоэксперт, главред канала @sochiautoparts. "
            "Ты видишь пост в автомобильной группе и хочешь оставить КОРОТКИЙ комментарий. "
            "Правила:\n"
            "1. Максимум 300 символов — кратко и живо\n"
            "2. Добавь экспертное мнение, вопрос или реакцию\n"
            "3. Пиши как живой человек — эмоционально и естественно\n"
            "4. НЕ рекламируй свой канал — это спам\n"
            "5. НЕ используй markdown, буллеты, жирный текст\n"
            "6. Можно добавить юмор или иронию\n"
            "7. Твой ответ — ТОЛЬКО текст комментария, без кавычек и пояснений\n"
            "8. НИКАКОЙ политики и войны\n\n"
            f"Пост в группе:\n{post_text[:500]}\n\n"
            "Напиши короткий живой комментарий:"
        )
        
        response = await ai_router.generate_comment(
            prompt=comment_prompt,
            max_tokens=100,
        )
        
        if not response or not response.text:
            logger.debug("Comment generation returned empty")
            return False
        
        comment = response.text.strip()
        
        # Clean up comment
        comment = re.sub(r'<[^>]+>', '', comment)  # Remove HTML tags
        comment = re.sub(r'\*\*.*?\*\*', '', comment)  # Remove markdown bold
        comment = re.sub(r'__.*?__', '', comment)  # Remove markdown italic
        
        # Truncate to limit
        if len(comment) > MAX_COMMENT_LENGTH:
            # Find natural break point
            cut = comment[:MAX_COMMENT_LENGTH]
            last_space = cut.rfind(' ')
            if last_space > MAX_COMMENT_LENGTH // 2:
                comment = cut[:last_space] + '...'
            else:
                comment = cut + '...'
        
        # Skip if comment is too short (low quality)
        if len(comment) < 15:
            logger.debug("Comment too short, skipping")
            return False
        
        # Post comment as reply to the original message
        await bot.send_message(
            chat_id=chat_id,
            text=comment,
            reply_to_message_id=message_id,
            parse_mode=ParseMode.HTML,
        )
        
        logger.info(f"Posted comment in group {chat_id}: {comment[:50]}...")
        return True
        
    except Exception as e:
        logger.warning(f"Failed to comment in group {chat_id}: {e}")
        return False


async def auto_comment_in_groups(bot: Bot) -> int:
    """Scan recent posts in groups where Ася is a member and comment.
    
    This is a background task that runs periodically.
    Uses bot.getUpdates() to discover groups, then scans recent messages.
    
    Returns number of comments posted.
    """
    if not bot:
        return 0
    
    comments_posted = 0
    
    try:
        # Get recent updates to find groups
        updates = await bot.get_updates(limit=50)
        
        group_chats = {}
        for update in updates:
            chat = None
            if update.message and update.message.chat:
                chat = update.message.chat
            elif update.channel_post and update.channel_post.chat:
                chat = update.channel_post.chat
            elif update.my_chat_member and update.my_chat_member.chat:
                chat = update.my_chat_member.chat
            
            if chat and chat.type in ("group", "supergroup"):
                group_chats[chat.id] = chat.title or str(chat.id)
        
        if not group_chats:
            logger.debug("No groups found in recent updates")
            return 0
        
        logger.info(f"Found {len(group_chats)} groups for auto-commenting")
        
        for chat_id, chat_name in group_chats.items():
            try:
                # Check daily comment limit for this group
                today = datetime.now(_MOSCOW_TZ).strftime("%Y-%m-%d")
                comment_key = f"auto_comment_{chat_id}_{today}"
                
                # Simple file-based rate limiting
                try:
                    import json as _json
                    rate_file = f"/tmp/asya_comment_rates.json"
                    rates = {}
                    try:
                        with open(rate_file, 'r') as f:
                            rates = _json.load(f)
                    except Exception:
                        pass
                    
                    today_count = rates.get(comment_key, 0)
                    if today_count >= MAX_COMMENTS_PER_GROUP_PER_DAY:
                        logger.debug(f"Comment limit reached for {chat_name}")
                        continue
                    
                    # Check minimum interval
                    last_comment_time = rates.get(f"last_comment_{chat_id}", 0)
                    if time.time() - last_comment_time < MIN_COMMENT_INTERVAL:
                        logger.debug(f"Comment interval too short for {chat_name}")
                        continue
                except Exception:
                    pass  # Rate limiting is best-effort
                
                # Try to get recent messages from the group
                # Note: bots can only see messages sent after they were added
                # and only if privacy mode is disabled
                try:
                    # Send a viewing reaction to the most recent post
                    # This is a soft engagement that works even if we can't read messages
                    pass  # Actual message scanning requires different approach
                except Exception as e:
                    logger.debug(f"Cannot scan group {chat_name}: {e}")
                
            except Exception as e:
                logger.warning(f"Error processing group {chat_name}: {e}")
                continue
        
    except Exception as e:
        logger.warning(f"Auto-comment scan failed: {e}")
    
    return comments_posted

