"""Asya Partner Integration — admitad_ads.json loader and link formatter.

Loads partner programs from
    https://github.com/creastudioai-beep/pr/blob/main/data/admitad_ads.json
Periodically updates, filters by region and category, formats links naturally.

Partner link format: [Brand–Category](URL) — looks natural in content
Example: [Роско–Автозапчасти](https://ujhjj.com/g/...)
"""

import json
import logging
import time
from pathlib import Path
from typing import Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

ADMITAD_JSON_URL = (
    "https://raw.githubusercontent.com/creastudioai-beep/pr/main/data/admitad_ads.json"
)
LOCAL_CACHE_PATH = Path("data/admitad_cache.json")

# ---------------------------------------------------------------------------
# Category mapping for natural link formatting
# ---------------------------------------------------------------------------
CATEGORY_LABELS = {
    "autoparts": "Автозапчасти",
    "Шины и диски": "Шины и диски",
    "Автострахование": "Страхование",
    "Проверка авто": "Проверка авто",
    "Прокат авто": "Прокат авто",
    "Инструменты": "Инструменты",
    "Купоны и скидки": "Акции",
    "Другое": "",
}

# Auto-relevant categories for filtering
AUTO_CATEGORIES = [
    "autoparts",
    "Автозапчасти",
    "Шины и диски",
    "Автострахование",
    "Проверка авто",
    "Прокат авто",
    "Инструменты",
]

# ---------------------------------------------------------------------------
# Brand display names for partner links
# ---------------------------------------------------------------------------
BRAND_DISPLAY_NAMES = {
    "Autopiter": "Autopiter",
    "Autopiter KZ": "Autopiter–KZ",
    "Rossko RU": "Роско",
    "BS-Tyres": "BS-Tyres",
    "Euro-diski": "Евродиски",
    "AvtoALL": "АвтоВСЁ",
    "Petrolplus": "Petrolplus",
    "Avtocod RU": "Автокод",
    "hyperauto.ru": "HyperAuto",
    "Globaldrive [CPS] RU": "GlobalDrive",
    "Колесо  RU": "Колесо",
    "Lukoil Shop RU": "Лукойл",
    "DiscoverCars WW": "DiscoverCars",
    "AliExpress RU&CIS": "AliExpress",
    "Alibaba WW": "Alibaba",
    "RAKETA - Быстрая доставка товаров из Китая RU": "Ракета",
}

# ---------------------------------------------------------------------------
# Runtime cache
# ---------------------------------------------------------------------------
_partner_programs: List[Dict] = []
_last_load_time: float = 0
_LOAD_INTERVAL = 3600  # Refresh every hour


# ===========================================================================
# Public helpers
# ===========================================================================

def format_partner_link(brand_name: str, category: str, goto_link: str) -> str:
    """Format a partner link in the natural ``[Brand–Category](URL)`` format.

    Args:
        brand_name: Raw brand name from admitad data.
        category: Category key from admitad data.
        goto_link: Affiliate URL.

    Returns:
        Markdown-formatted partner link string.
    """
    display_name = BRAND_DISPLAY_NAMES.get(brand_name, brand_name.split()[0])
    category_label = CATEGORY_LABELS.get(category, category if category else "")

    if category_label:
        return f"[{display_name}–{category_label}]({goto_link})"
    return f"[{display_name}]({goto_link})"


# ===========================================================================
# Data loading
# ===========================================================================

async def load_partner_programs(force: bool = False) -> List[Dict]:
    """Load partner programs from JSON URL or local cache.

    The data is cached in memory for ``_LOAD_INTERVAL`` seconds.  Set
    *force* to ``True`` to bypass the interval check.

    Returns:
        List of partner program dicts with keys: name, category,
        category_name, goto_link, allowed_regions, description, slug.
    """
    global _partner_programs, _last_load_time

    now = time.time()
    if not force and _partner_programs and (now - _last_load_time) < _LOAD_INTERVAL:
        return _partner_programs

    programs: List[Dict] = []

    # --- Try remote URL first -------------------------------------------
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=5.0)
        ) as client:
            response = await client.get(ADMITAD_JSON_URL)
            if response.status_code == 200:
                data = response.json()
                programs = data.get("programs", [])
                logger.info("Loaded %d partner programs from URL", len(programs))

                # Persist to local file cache
                try:
                    LOCAL_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
                    with open(LOCAL_CACHE_PATH, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                except Exception as exc:
                    logger.warning("Failed to cache admitad data: %s", exc)
    except Exception as exc:
        logger.warning("Failed to load partner programs from URL: %s", exc)

    # --- Fallback to local file cache -----------------------------------
    if not programs and LOCAL_CACHE_PATH.exists():
        try:
            with open(LOCAL_CACHE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                programs = data.get("programs", [])
                logger.info("Loaded %d partner programs from cache", len(programs))
        except Exception as exc:
            logger.warning("Failed to load partner programs from cache: %s", exc)

    _partner_programs = programs
    _last_load_time = now
    return programs


# ===========================================================================
# Search & filtering
# ===========================================================================

def find_partner_links(
    query: str,
    region: str = "RU",
    max_results: int = 3,
) -> List[Dict]:
    """Find relevant partner links for a query.

    Args:
        query: Search query (e.g. "автозапчасти", "шины", "масло").
        region: Target region code (default ``"RU"``).
        max_results: Maximum number of results to return.

    Returns:
        List of dicts with keys: name, category_name, goto_link,
        formatted_link, regions.
    """
    if not _partner_programs:
        return []

    query_lower = query.lower()
    results: List[Dict] = []

    # Keyword → category mapping
    auto_keywords = {
        "запчаст": ["autoparts", "Автозапчасти"],
        "автозапчаст": ["autoparts", "Автозапчасти"],
        "детал": ["autoparts", "Автозапчасти"],
        "шин": ["Шины и диски"],
        "диск": ["Шины и диски"],
        "колес": ["Шины и диски"],
        "страх": ["Автострахование"],
        "осаго": ["Автострахование"],
        "каск": ["Автострахование"],
        "провер": ["Проверка авто"],
        "vin": ["Проверка авто"],
        "истор": ["Проверка авто"],
        "прокат": ["Прокат авто"],
        "аренд": ["Прокат авто"],
        "инструмент": ["Инструменты"],
        "масл": ["autoparts", "Автозапчасти"],
        "фильтр": ["autoparts", "Автозапчасти"],
        "тормоз": ["autoparts", "Автозапчасти"],
    }

    target_categories: set = set()
    for kw, cats in auto_keywords.items():
        if kw in query_lower:
            target_categories.update(cats)

    # If no specific category match, include all auto categories
    if not target_categories:
        target_categories = set(AUTO_CATEGORIES)

    for prog in _partner_programs:
        if len(results) >= max_results:
            break

        prog_category = prog.get("category_name", prog.get("category", ""))
        prog_regions = prog.get("allowed_regions", [])
        goto_link = prog.get("goto_link", "")

        # Check category match
        if not any(
            cat in prog_category or cat in prog.get("category", "")
            for cat in target_categories
        ):
            # Also check if program name matches query
            prog_name = prog.get("name", "").lower()
            if not any(kw in prog_name for kw in query_lower.split()):
                continue

        # Check region
        if region and "00" not in prog_regions and region not in prog_regions:
            continue

        if not goto_link:
            continue

        brand_name = prog.get("name", "")
        formatted = format_partner_link(brand_name, prog_category, goto_link)

        results.append(
            {
                "name": brand_name,
                "category_name": prog_category,
                "goto_link": goto_link,
                "formatted_link": formatted,
                "regions": prog_regions,
            }
        )

    return results


def get_auto_partner_links(region: str = "RU") -> List[Dict]:
    """Get all auto-related partner links for a region.

    Returns formatted partner links for use in channel posts and chat.
    """
    return find_partner_links(
        "автозапчасти шины страхование", region=region, max_results=10
    )


def get_daily_partner_post_links() -> List[str]:
    """Get partner links for a daily partner post.

    Selects a mix of auto-related partners to feature in a daily post.
    """
    links = find_partner_links(
        "автозапчасти шины диски масло фильтры", region="RU", max_results=5
    )
    if not links:
        return []
    return [link["formatted_link"] for link in links]


def inject_partner_links(
    text: str,
    query: str = "",
    region: str = "RU",
) -> str:
    """Inject relevant partner links into text naturally.

    Adds 1-2 partner links at the end if relevant to the content.

    Args:
        text: The original text to augment.
        query: Optional explicit search query.  If empty, the first 50
            characters of *text* are used as a heuristic.
        region: Target region code.

    Returns:
        Text with appended partner link block, or the original text
        unchanged if no relevant partners were found.
    """
    links = find_partner_links(
        query or text[:50], region=region, max_results=2
    )
    if not links:
        return text

    partner_text = "Где найти: " + " | ".join(
        link["formatted_link"] for link in links
    )
    return f"{text}\n\n{partner_text}"
