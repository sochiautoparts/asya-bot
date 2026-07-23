"""
Ася Post Utilities — smart truncation, text cleaning, validation, dedup, translation.

Restored from pre-OpenClaw architecture (channel.py monolith) to ensure:
- Footer always fits within Telegram limits (smart truncation, not hard cut)
- AI output is cleaned (no markdown, no prompt leakage, no "Ася:" prefix)
- Posts are validated (no politics/NSFW/irrelevant content)
- English news is translated to Russian
- Images are validated (magic bytes, not just size check)
- Duplicate posts are prevented (URL + title fingerprint + text hash)
"""

import re
import hashlib
import logging
from typing import Optional, Tuple

logger = logging.getLogger("asya.post_utils")

# ─── Smart Truncation ───────────────────────────────────────────────────────

def smart_truncate(text: str, limit: int, footer_len: int = 0) -> str:
    """Truncate text to fit within limit (including footer), at natural boundary.

    Tries (in order): paragraph break > sentence end > newline > word boundary.
    Appends "…" if truncated. Reserves space for footer.
    """
    if not text:
        return ""
    effective_limit = limit - footer_len - 3  # 3 for "…"
    if effective_limit < 50:
        effective_limit = 50
    if len(text) <= effective_limit:
        return text

    # Try paragraph break (double newline)
    for i in range(effective_limit, max(effective_limit - 200, 0), -1):
        if i < len(text) and text[i:i+2] == "\n\n":
            return text[:i].rstrip() + "…"

    # Try sentence end (. ! ? …)
    for i in range(effective_limit, max(effective_limit - 200, 0), -1):
        if i < len(text) and text[i] in ".!?\n" and (i + 1 >= len(text) or text[i+1] in " \n\t"):
            return text[:i+1].rstrip() + "…"

    # Try newline
    for i in range(effective_limit, max(effective_limit - 100, 0), -1):
        if i < len(text) and text[i] == "\n":
            return text[:i].rstrip() + "…"

    # Try word boundary (space)
    for i in range(effective_limit, max(effective_limit - 50, 0), -1):
        if i < len(text) and text[i] == " ":
            return text[:i].rstrip() + "…"

    # Last resort: hard cut
    return text[:effective_limit].rstrip() + "…"


# ─── Text Cleaning ──────────────────────────────────────────────────────────

_BANNED_OPENINGS = [
    "ася:", "маша:", "редакция:", "привет", "здравствуй", "всем привет",
    "добрый день", "доброе утро", "добрый вечер",
]

_PROMPT_LEAKAGE_PATTERNS = [
    r"^напиши\s+пост",
    r"^напиши\s+комментар",
    r"^стиль\s*[(:]",
    r"^заголовок\s+новости",
    r"^краткое\s+содержание",
    r"^не\s+копируй",
    r"^не\s+добавляй",
    r"^не\s+начинай",
    r"^женский\s+род",
    r"^по-русски",
    r"^-{2,}\s*$",
]

_MARKDOWN_PATTERNS = [
    (r"\*\*(.+?)\*\*", r"\1"),
    (r"__(.+?)__", r"\1"),
    (r"\*(.+?)\*", r"\1"),
    (r"_(.+?)_", r"\1"),
    (r"`(.+?)`", r"\1"),
    (r"^#{1,6}\s+", ""),
    (r"^>\s+", ""),
    (r"^[-*]\s+", "• "),
]

_DISCLAIMER_PATTERNS = [
    r"как\s+искусственный\s+интеллект",
    r"я\s+не\s+(?:могу|имею\s+доступ)",
    r"у\s+меня\s+нет\s+доступа",
    r"обратите\s+внимание.*?(?:источник|оригинал)",
    r"(?:данный|этот)\s+(?:текст|материал)\s+(?:является|представляет)",
    r"информация\s+(?:предоставлена|взята|из\s+источника)",
]


def clean_post_text(text: str, bot_name: str = "Ася") -> str:
    """Clean AI-generated text: strip markdown, disclaimers, prompt leakage, name prefixes."""
    if not text:
        return ""

    lines = text.strip().split("\n")
    cleaned_lines = []

    for line in lines:
        line_stripped = line.strip()

        if not line_stripped:
            if cleaned_lines and cleaned_lines[-1]:
                cleaned_lines.append("")
            continue

        # Skip prompt leakage lines
        is_leakage = False
        for pattern in _PROMPT_LEAKAGE_PATTERNS:
            if re.match(pattern, line_stripped, re.IGNORECASE):
                is_leakage = True
                break
        if is_leakage:
            continue

        # Skip disclaimer lines
        is_disclaimer = False
        for pattern in _DISCLAIMER_PATTERNS:
            if re.search(pattern, line_stripped, re.IGNORECASE):
                is_disclaimer = True
                break
        if is_disclaimer:
            continue

        # Strip markdown
        for pattern, replacement in _MARKDOWN_PATTERNS:
            line_stripped = re.sub(pattern, replacement, line_stripped)

        # Strip "Name:" prefix from start of each line
        for opening in _BANNED_OPENINGS:
            if line_stripped.lower().startswith(opening):
                line_stripped = line_stripped[len(opening):].lstrip(" ,!.—-:")
                break

        cleaned_lines.append(line_stripped)

    text = "\n".join(cleaned_lines).strip()

    # Remove banned openings (case-insensitive)
    for opening in _BANNED_OPENINGS:
        if text.lower().startswith(opening):
            text = text[len(opening):].lstrip(" ,!.—-:")
            break

    # Normalize whitespace
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)

    # Fix space before punctuation
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)

    # Fix missing space after punctuation (but not for URLs/numbers)
    text = re.sub(r"([,.;:!?])([^\s\d\n.])", r"\1 \2", text)

    return text.strip()


# ─── Validation ─────────────────────────────────────────────────────────────

_POLITICS_KEYWORDS = [
    "путин", "кремл", "госдум", "санкци", "мобилиз",
    "зеленск", "байден", "трамп", "выборы", "парламент", "ракетн", "обстрел",
    "минобор", "днр", "лнр",
    # "сво" — only match as standalone Russian abbreviation (not Spanish "sitio", "sivo")
    "сво ", " сво ", 
    # "война" — only with context (standalone war, not "войната" in other languages)
    " война ", 
]

_NSFW_KEYWORDS = [
    "порн", "эрот", "секс", "18+", "nsfw",
]

_AUTO_KEYWORDS = [
    "авто", "машин", "мотор", "двигат", "колес", "шин", "кузов", "седан",
    "кроссовер", "внедорож", "хэтчб", "универсал", "купе", "кабриолет",
    "bmw", "бмв", "mercedes", "мерседес", "audi", "ауди", "vw", "фольксваген",
    "toyota", "тойот", "ниссан", "nissan", "honda", "хонд", "hyundai", "kia",
    "mazda", "мазда", "lada", "лада", "porsche", "порше",
    "ev", "электромобил", "гибрид", "заряд", "батарей", "л.с.", "км/ч",
    "ньютон", "н·м", "турбо", "twinpower", "quattro", "xdrive",
    "запчаст", "детал", "сервис", "ремонт", "масл", "фильтр",
    "drive", "sedan", "suv", "coupe", "engine", "horsepower", "torque",
    "speed", "power", "wheel", "tire", "brake", "chevy", "blazer",
]


# ─── Language Detection ────────────────────────────────────────────────────

_CYRILLIC_RE = re.compile(r"[а-яё]", re.IGNORECASE)
_LATIN_RE = re.compile(r"[a-z]", re.IGNORECASE)


def validate_post_text(text: str, require_keywords: list = None) -> Tuple[bool, str]:
    """Validate post text. Returns (is_valid, reason)."""
    if not text or len(text) < 50:
        return False, "too_short"

    t = text.lower()

    for kw in _POLITICS_KEYWORDS:
        if kw in t:
            return False, f"politics:{kw}"

    for kw in _NSFW_KEYWORDS:
        if kw in t:
            return False, f"nsfw:{kw}"

    keywords = require_keywords or _AUTO_KEYWORDS
    has_relevant = any(kw.lower() in t for kw in keywords)
    if not has_relevant:
        return False, "not_auto_relevant"

    # Language check: post MUST be in Russian (cyrillic chars > latin)
    cyrillic = len(_CYRILLIC_RE.findall(text))
    latin = len(_LATIN_RE.findall(text))
    if latin > cyrillic * 2:
        return False, "not_russian"

    return True, "ok"


def detect_language(text: str) -> str:
    """Detect if text is primarily Russian (ru) or English/other (en)."""
    if not text:
        return "ru"
    cyrillic = len(_CYRILLIC_RE.findall(text))
    latin = len(_LATIN_RE.findall(text))
    return "ru" if cyrillic > latin else "en"


def needs_translation(title: str, summary: str) -> bool:
    """Check if news title/summary is in English and needs translation."""
    combined = f"{title} {summary}"
    return detect_language(combined) == "en"


# ─── Image Validation ───────────────────────────────────────────────────────

_IMAGE_MAGIC_BYTES = {
    b"\xff\xd8\xff": "jpeg",
    b"\x89PNG\r\n\x1a\n": "png",
    b"RIFF": "webp",
    b"GIF8": "gif",
}


def validate_image(content: bytes) -> bool:
    """Validate image by magic bytes. Returns True if valid photo (JPEG/PNG/WebP)."""
    if not content or len(content) < 1024:
        return False
    for magic, fmt in _IMAGE_MAGIC_BYTES.items():
        if content[:len(magic)] == magic:
            if fmt == "gif":
                return False
            if fmt == "webp" and content[8:12] != b"WEBP":
                return False
            return True
    return False


def is_svg(content: bytes) -> bool:
    """Check if content is an SVG image (by magic bytes or content-type)."""
    if not content:
        return False
    # SVG magic: <?xml or <svg
    head = content[:200].lower()
    return b"<svg" in head or b"<?xml" in head and b"svg" in head


def convert_svg_to_png(svg_content: bytes, output_width: int = 512) -> bytes:
    """Convert SVG image to PNG bytes using cairosvg.
    Returns PNG bytes or empty bytes on failure.
    """
    try:
        import cairosvg
        png_bytes = cairosvg.svg2png(
            bytestring=svg_content,
            output_width=output_width,
            output_height=output_width,
        )
        return png_bytes
    except Exception as e:
        logger.warning(f"SVG→PNG conversion failed: {e}")
        return b""


def prepare_partner_logo(content: bytes) -> bytes:
    """Prepare partner logo for Telegram send_photo.
    If SVG — convert to PNG. If already JPEG/PNG/WebP — return as-is.
    Returns empty bytes if cannot prepare.
    """
    if not content:
        return b""
    # If already valid image — return as-is
    if validate_image(content):
        return content
    # If SVG — convert to PNG
    if is_svg(content):
        png = convert_svg_to_png(content)
        if png and validate_image(png):
            return png
    return b""


# ─── Deduplication ──────────────────────────────────────────────────────────

def title_fingerprint(title: str) -> str:
    """Create a normalized fingerprint from a news title for dedup."""
    t = re.sub(r"[^\w\sа-яё]", "", (title or "").lower())
    t = re.sub(r"\s+", " ", t).strip()
    words = [w for w in t.split() if len(w) > 2][:5]
    return " ".join(words)


# Key entities for topic dedup (brand + model keywords that indicate same event)
_TOPIC_ENTITIES = [
    # Ferrari
    "ferrari", "феррари", "12cilindri", "manuale", "355 gts",
    # Skoda/VW
    "kodiaq", "tayron", "skoda", "шкода",
    # BMW models
    "m3", "m4", "m5", "m8", "ix5", "ix7", "ix3", "x5", "x7",
    "alpina", "b58", "s63", "n55",
    # Events
    "norisring", "dtm", "нюрбургринг", "nurburgring",
    # Furniture brands (Даша)
    "kerama", "marazzi", "ikea", "misladen",
]


def topic_fingerprint(title: str, summary: str = "") -> str:
    """Extract topic fingerprint from title+summary for semantic dedup.
    Returns a normalized string of key entities found (e.g. 'ferrari manuale').
    Two news items with same topic fingerprint are about the SAME event.
    """
    combined = f"{title} {summary}".lower()
    found = []
    for entity in _TOPIC_ENTITIES:
        if entity in combined and entity not in found:
            found.append(entity)
    return " ".join(sorted(found))


def text_fingerprint(text: str) -> str:
    """Create MD5 hash of normalized text for dedup."""
    t = re.sub(r"[^\w\sа-яё]", "", (text or "").lower())
    t = re.sub(r"\s+", " ", t).strip()
    return hashlib.md5(t.encode("utf-8")).hexdigest()


def url_normalize(url: str) -> str:
    """Normalize URL for dedup (strip query params, trailing slash)."""
    if not url:
        return ""
    u = re.sub(r"^https?://", "", url.lower())
    u = re.sub(r"^www\.", "", u)
    u = u.split("?")[0].split("#")[0]
    u = u.rstrip("/")
    return u


# ─── Date Context ───────────────────────────────────────────────────────────

from datetime import datetime, timezone, timedelta

_MOSCOW_TZ = timezone(timedelta(hours=3))


def date_context() -> str:
    """Return current date in Russian for prompt context."""
    now = datetime.now(_MOSCOW_TZ)
    months = [
        "января", "февраля", "марта", "апреля", "мая", "июня",
        "июля", "августа", "сентября", "октября", "ноября", "декабря"
    ]
    weekdays = [
        "понедельник", "вторник", "среда", "четверг",
        "пятница", "суббота", "воскресенье"
    ]
    return f"сегодня {now.day} {months[now.month - 1]} {now.year} года, {weekdays[now.weekday()]}"


# ─── Uniquification Rules ───────────────────────────────────────────────────

UNIQUIFICATION_RULES = """
УНИКАЛИЗАЦИЯ (обязательно):
1. Не копируй заголовок или текст новости — пиши СВОЙ комментарий
2. Используй другие слова, другую структуру предложений
3. Добавь личное мнение/анализ от лица редакции
4. Меняй порядок мыслей, добавляй контекст
5. Не используй прямые цитаты из новости
6. Добавляй технические детали (л.с., Н·м, км/ч) которых нет в источнике
7. Меняй тон и угол подачи (экономика, техника, эмоции, сравнение)"""
