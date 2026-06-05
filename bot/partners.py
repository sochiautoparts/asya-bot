"""
Admitad Partner Program Integration v3.0

Loads partner data from remote admitad_ads.json (updateable file!).
Uses goto_link EXACTLY as-is — no subid additions, no modifications.
The goto_links are ready for both posts and user dialogs.

Key changes from v2:
- Downloads admitad_ads.json from remote GitHub URL
- Auto-refreshes every 6 hours (file is updateable!)
- Uses goto_link EXACTLY as provided — NO subid additions
- Regional filtering by allowed_regions
- For article searches, modifies ulp parameter in goto_link
- Proper formatting: "Name (category description): goto_link"
"""

import json
import random
import re
import time
import logging
import asyncio
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from urllib.parse import quote_plus, urlparse, parse_qs, urlencode, urlunparse

from bot.config import config

logger = logging.getLogger("asya.partners")

# Remote admitad_ads.json URL (updateable file!)
ADMITAD_JSON_URL = "https://raw.githubusercontent.com/creastudioai-beep/pr/main/data/admitad_ads.json"
ADMITAD_LOCAL_CACHE = "data/admitad_ads.json"
ADMITAD_REFRESH_INTERVAL = 6 * 3600  # Refresh every 6 hours

# Default region for partner filtering
DEFAULT_REGION = "RU"


class PartnerProgram:
    """Single partner program from admitad."""

    def __init__(self, data: Dict):
        self.id = str(data.get("id", ""))
        self.name = data.get("name", "")
        self.slug = data.get("slug", "")
        self.image = (
            data.get("image") or
            data.get("image_url") or
            data.get("logo") or
            data.get("brand_logo") or
            ""
        )
        self.description = data.get("description", "")
        self.ad_text = data.get("ad_text", "")
        self.goto_link = data.get("goto_link", "")
        self.site_url = data.get("site_url", "")
        self.category = data.get("category", "")
        self.category_name = data.get("category_name", "")
        self.allowed_regions = data.get("allowed_regions", [])
        self.rating = data.get("rating", "")
        self.raw = data

    def has_region(self, region: str = DEFAULT_REGION) -> bool:
        """Check if program is available in a region."""
        if not self.allowed_regions:
            return True  # No region info = available everywhere
        region_upper = region.upper()
        # "00" means worldwide
        if "00" in self.allowed_regions:
            return True
        return region_upper in [r.upper() for r in self.allowed_regions]

    def has_category(self, category: str) -> bool:
        """Check if program belongs to a category."""
        cat_lower = category.lower()
        if cat_lower == self.category.lower():
            return True
        if cat_lower in self.category_name.lower():
            return True
        return False

    def matches_text(self, text: str) -> bool:
        """Check if text contains keywords related to this program."""
        text_lower = text.lower()
        # Check program name words
        name_words = [w.lower() for w in self.name.split() if len(w) > 3]
        for word in name_words:
            if word in text_lower:
                return True
        # Check category name words
        cat_words = [w.lower() for w in self.category_name.split() if len(w) > 3]
        for word in cat_words:
            if word in text_lower:
                return True
        # Check site_url domain
        if self.site_url:
            domain = urlparse(self.site_url).netloc.replace("www.", "")
            if domain and domain in text_lower:
                return True
        return False

    def get_search_url(self, query: str) -> str:
        """Get a search URL for this partner, using the goto_link as base.

        If the goto_link has a ulp parameter (redirect URL), we modify it
        to include the search path. Otherwise, returns the goto_link as-is.
        """
        if not self.goto_link:
            return ""
        if not query:
            return self.goto_link

        # Try to modify the ulp parameter to include search
        try:
            parsed = urlparse(self.goto_link)
            params = parse_qs(parsed.query)

            if "ulp" in params and params["ulp"]:
                original_ulp = params["ulp"][0]
                # Build search URL based on site_url patterns
                search_url = self._build_search_url(original_ulp, query)
                if search_url != original_ulp:
                    # Replace ulp parameter
                    new_params = {}
                    for k, v_list in params.items():
                        if k == "ulp":
                            new_params[k] = search_url
                        else:
                            new_params[k] = v_list[0] if len(v_list) == 1 else v_list

                    # Rebuild URL with new ulp
                    new_query = urlencode(new_params, doseq=True)
                    return urlunparse(parsed._replace(query=new_query))
        except Exception as e:
            logger.debug(f"Error modifying goto_link for search: {e}")

        # If we can't modify, return as-is
        return self.goto_link

    def _build_search_url(self, original_ulp: str, query: str) -> str:
        """Build a search URL by modifying the original redirect URL."""
        site_url = self.site_url.rstrip("/")
        query_encoded = quote_plus(query)

        # Site-specific search URL patterns
        search_patterns = {
            "rossko.ru": f"{site_url}/search?text={query_encoded}",
            "autopiter.ru": f"{site_url}/search?querystr={query_encoded}",
            "autopiter.kz": f"{site_url}/search?querystr={query_encoded}",
            "exist.ru": f"{site_url}/Price/?p={query_encoded}",
            "emex.ru": f"{site_url}/products?search={query_encoded}",
            "autodoc.ru": f"{site_url}/search?keyword={query_encoded}",
            "zzap.ru": f"{site_url}/search/?q={query_encoded}",
            "avtoall.ru": f"{site_url}/search/?q={query_encoded}",
            "aliexpress.ru": f"{site_url}/wholesale?SearchText={query_encoded}",
            "aliexpress.com": f"{site_url}/wholesale?SearchText={query_encoded}",
            "hyperauto.ru": f"{site_url}/search/?q={query_encoded}",
            "euro-diski.ru": f"{site_url}/search/?q={query_encoded}",
            "bs-tyres.ru": f"{site_url}/search/?q={query_encoded}",
            "koleso.ru": f"{site_url}/search/?q={query_encoded}",
            "avtocod.ru": f"{site_url}/search/?q={query_encoded}",
            "petrolplus.ru": f"{site_url}/search/?q={query_encoded}",
            "globaldrive.ru": f"{site_url}/search/?q={query_encoded}",
            "mirdvornikov.ru": f"{site_url}/search/?q={query_encoded}",
            "lukoil-shop.com": f"{site_url}/search/?q={query_encoded}",
        }

        # Check if site_url matches any pattern
        for domain, pattern in search_patterns.items():
            if domain in self.site_url:
                return quote_plus(pattern)

        # Generic fallback: just add search parameter
        return original_ulp

    def format_link(self, with_description: bool = True) -> str:
        """Format this partner's link for display.

        Uses goto_link EXACTLY as-is from the file.
        No subid additions — the link is ready to use!
        """
        if not self.goto_link:
            return ""

        if with_description and self.category_name:
            return f"{self.name} ({self.category_name}): {self.goto_link}"
        return f"{self.name}: {self.goto_link}"

    def format_link_with_search(self, query: str) -> str:
        """Format this partner's link with a search query.

        Modifies the ulp parameter in goto_link to include search.
        The base goto_link (with tracking) is preserved.
        """
        search_url = self.get_search_url(query)
        if not search_url:
            return ""

        if self.category_name:
            # Add helpful description based on category
            desc = self._get_category_description()
            return f"{self.name} ({desc}): {search_url}"
        return f"{self.name}: {search_url}"

    def _get_category_description(self) -> str:
        """Get a user-friendly description for this partner's category."""
        descriptions = {
            "autoparts": "профессиональный подбор запчастей",
            "tires": "шины и диски",
            "tools": "автоинструменты",
            "autoinsurance": "автострахование",
            "checkauto": "проверка авто",
            "autorent": "аренда авто",
            "coupons": "скидки и промокоды",
            "other": "рекомендую",
        }
        return descriptions.get(self.category, self.category_name or "рекомендую")


class PartnerManager:
    """Manages all partner programs — loading, matching, posting.

    v3.0: Downloads admitad_ads.json from remote URL, auto-refreshes.
    Uses goto_link EXACTLY as-is — NO subid additions!
    """

    def __init__(self):
        self.programs: List[PartnerProgram] = []
        self._loaded = False
        self._last_load_time: float = 0
        self._last_post_time: float = 0
        self._posted_today: int = 0
        self._day_start: float = 0
        # Site URL → PartnerProgram mapping for fast lookup
        self._site_map: Dict[str, PartnerProgram] = {}

    async def load_async(self) -> int:
        """Load partner programs — try remote first, then local cache."""
        count = await self._load_from_remote()
        if count > 0:
            return count
        # Fallback to local cache
        return self._load_from_local()

    async def _load_from_remote(self) -> int:
        """Download admitad_ads.json from GitHub."""
        try:
            import httpx
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                response = await client.get(ADMITAD_JSON_URL)
                if response.status_code == 200:
                    data = response.json()
                    count = self._parse_programs(data)
                    if count > 0:
                        # Save to local cache
                        self._save_cache(data)
                        self._loaded = True
                        self._last_load_time = time.time()
                        logger.info(f"Loaded {count} partner programs from remote URL")
                        return count
        except Exception as e:
            logger.warning(f"Failed to load admitad_ads.json from remote: {e}")
        return 0

    def _load_from_local(self) -> int:
        """Load from local cache file."""
        # Try data/ directory first, then root
        for filepath in [ADMITAD_LOCAL_CACHE, "admitad_ads.json"]:
            path = Path(filepath)
            if path.exists():
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    count = self._parse_programs(data)
                    self._loaded = True
                    self._last_load_time = time.time()
                    logger.info(f"Loaded {count} partner programs from local cache: {filepath}")
                    return count
                except Exception as e:
                    logger.error(f"Error loading local admitad cache: {e}")
        logger.warning("No admitad_ads.json found locally or remotely")
        self._loaded = True
        return 0

    def load(self, filepath: str = "") -> int:
        """Synchronous load from local file only."""
        filepath = filepath or config.ADMITAD_ADS_FILE
        path = Path(filepath)
        if not path.exists():
            path = Path(ADMITAD_LOCAL_CACHE)
        if not path.exists():
            path = Path("admitad_ads.json")

        if not path.exists():
            logger.warning(f"Partner ads file not found")
            self._loaded = True
            return 0

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            count = self._parse_programs(data)
            self._loaded = True
            self._last_load_time = time.time()
            logger.info(f"Loaded {count} partner programs from {path}")
            return count
        except Exception as e:
            logger.error(f"Error loading partner ads: {e}")
            self._loaded = True
            return 0

    def _parse_programs(self, data) -> int:
        """Parse programs from JSON data."""
        self.programs = []
        self._site_map = {}

        items = []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get("programs", data.get("items", data.get("results", [])))
            if not isinstance(items, list):
                items = []

        for item in items:
            prog = PartnerProgram(item)
            if prog.goto_link:
                self.programs.append(prog)
                # Build site URL mapping
                if prog.site_url:
                    domain = urlparse(prog.site_url).netloc.replace("www.", "")
                    self._site_map[domain] = prog

        return len(self.programs)

    def _save_cache(self, data) -> None:
        """Save data to local cache."""
        try:
            cache_path = Path(ADMITAD_LOCAL_CACHE)
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"Failed to save admitad cache: {e}")

    def ensure_loaded(self) -> None:
        """Load partner programs if not yet loaded."""
        if not self._loaded:
            self.load()

    async def maybe_refresh(self) -> None:
        """Refresh from remote if enough time has passed."""
        if not self._loaded or (time.time() - self._last_load_time > ADMITAD_REFRESH_INTERVAL):
            await self.load_async()

    def get_by_category(self, category: str, region: str = DEFAULT_REGION) -> List[PartnerProgram]:
        """Get programs in a specific category and region."""
        self.ensure_loaded()
        return [p for p in self.programs if p.has_category(category) and p.has_region(region)]

    def get_by_site(self, site_url: str) -> Optional[PartnerProgram]:
        """Find a partner program by its site URL."""
        self.ensure_loaded()
        domain = urlparse(site_url).netloc.replace("www.", "") if site_url else ""
        return self._site_map.get(domain)

    def get_all_categories(self) -> List[str]:
        """Get all available categories across programs."""
        self.ensure_loaded()
        cats = set()
        for p in self.programs:
            if p.category:
                cats.add(p.category)
            if p.category_name:
                cats.add(p.category_name)
        return sorted(cats)

    def find_matching_programs(self, text: str, region: str = DEFAULT_REGION) -> List[PartnerProgram]:
        """Find programs that match keywords in the text."""
        self.ensure_loaded()
        text_lower = text.lower()

        matches = []
        # 1. Direct text matching
        for p in self.programs:
            if p.has_region(region) and p.matches_text(text):
                matches.append(p)

        # 2. Category keyword matching
        if not matches:
            category_keywords = {
                "autoparts": ["запчаст", "деталь", "артикул", "купить запчас", "купить детал",
                              "оригинал", "аналог", "замена", "подбор", "номер детал",
                              "oem", "оригинальн", "поиск запчас", "найти запчас",
                              "фильтр", "колодки", "свечи", "ремень", "прокладк",
                              "сальник", "подшипник", "амортизатор", "реле", "датчик",
                              "масло", "антифриз", "тормозн", "где купить",
                              "росско", "rossko", "autopiter", "автопитер"],
                "tires": ["шины", "диски", "резина", "колёса", "зимняя", "летняя",
                          "шипованные", "euro-diski", "bs-tyres"],
                "tools": ["инструмент", "ключ", "набор", "гараж", "домкрат", "avtoall"],
                "autoinsurance": ["страховка", "осаго", "каско", "страхование", "полис",
                                  "petrolplus", "avtocod"],
                "checkauto": ["проверка", "вин", "vin", "история", "автокод", "пробить",
                              "hyperauto"],
                "autorent": ["аренда", "прокат", "рент", "арендовать", "напрокат",
                             "discovercars", "localrent"],
                "coupons": ["промокод", "скидк", "купон", "акция", "aliexpress",
                            "globaldrive", "koleso", "mirdvornikov", "raketa"],
            }

            for cat, keywords in category_keywords.items():
                if any(kw in text_lower for kw in keywords):
                    cat_programs = self.get_by_category(cat, region)
                    matches.extend(cat_programs)
                    break

        return matches

    def get_random_program(self, category: str = "", region: str = DEFAULT_REGION) -> Optional[PartnerProgram]:
        """Get a random partner program, optionally filtered by category."""
        self.ensure_loaded()
        pool = self.get_by_category(category, region) if category else [p for p in self.programs if p.has_region(region)]
        return random.choice(pool) if pool else None

    def generate_partner_context(self, text: str, max_programs: int = 3, region: str = DEFAULT_REGION) -> str:
        """
        Generate context about matching partner programs for AI to reference
        naturally in its response.

        v3: Uses goto_link from admitad_ads.json EXACTLY as-is.
        No subid additions — the link is ready!
        """
        self.ensure_loaded()

        # Find matching programs
        matches = self.find_matching_programs(text, region)
        if not matches:
            # Default: show autoparts programs for auto-related queries
            text_lower = text.lower()
            auto_keywords = ["запчаст", "деталь", "артикул", "купить", "подобрать",
                             "масло", "фильтр", "колодки", "ремонт", "сто"]
            if any(kw in text_lower for kw in auto_keywords):
                matches = self.get_by_category("autoparts", region)

        if not matches:
            return ""

        # Check if there's an article number for search-specific links
        article_match = re.search(r'\b([A-Z0-9]{4,}[-/]?[A-Z0-9]*)\b', text.upper())
        article = article_match.group(1) if article_match else ""

        lines = [
            "Партнёрские ссылки (Вставь ЕСТЕСТВЕННО в ответ — как рекомендация, не как реклама!):",
        ]

        for p in matches[:max_programs]:
            if article:
                # For article searches, try to get a search-specific URL
                link = p.format_link_with_search(article)
            else:
                link = p.format_link(with_description=True)

            if link:
                lines.append(f"- {link}")

        # Also add Rossko specifically for auto parts queries
        text_lower = text.lower()
        parts_keywords = ["запчаст", "деталь", "артикул", "купить запчас", "подбор",
                          "фильтр", "колодки", "свечи", "ремень", "масло"]
        if any(kw in text_lower for kw in parts_keywords):
            rossko = self.get_by_site("rossko.ru")
            if rossko and rossko not in matches[:max_programs]:
                if article:
                    lines.append(f"- {rossko.format_link_with_search(article)}")
                else:
                    lines.append(f"- {rossko.format_link(with_description=True)}")

        if len(lines) <= 1:
            return ""

        lines.append("")
        lines.append("ВАЖНО: Ссылки выше — ПАРТНЁРСКИЕ (goto_link из admitad_ads.json). Используй их КАК ЕСТЬ, ничего не добавляй и не меняй!")

        return "\n".join(lines)

    def get_all_partner_links_for_parts(self, query: str, region: str = DEFAULT_REGION) -> List[Dict[str, str]]:
        """Get all partner links relevant to a parts query.

        Returns list of dicts with 'name', 'url', 'description' keys.
        Uses goto_link from admitad_ads.json.
        """
        links = []
        self.ensure_loaded()

        # Find autoparts programs
        parts_programs = self.get_by_category("autoparts", region)
        if not parts_programs:
            # Try finding by text matching
            parts_programs = self.find_matching_programs(query, region)

        # Check for article number
        article_match = re.search(r'\b([A-Z0-9]{4,}[-/]?[A-Z0-9]*)\b', query.upper())
        article = article_match.group(1) if article_match else query.strip()

        for p in parts_programs:
            search_url = p.get_search_url(article)
            desc = p._get_category_description()
            links.append({
                "name": p.name,
                "url": search_url,
                "description": f"{p.category_name} — {desc}",
            })

        # Also check tires, tools, checkauto categories for auto-related queries
        for cat in ["tires", "tools", "checkauto"]:
            cat_programs = self.get_by_category(cat, region)
            for p in cat_programs[:1]:  # One from each related category
                if p not in parts_programs:
                    search_url = p.get_search_url(article)
                    links.append({
                        "name": p.name,
                        "url": search_url,
                        "description": f"{p.category_name} — {p._get_category_description()}",
                    })

        return links

    def should_post_partner(self) -> bool:
        """Check if it's time to post a partner message to channel."""
        now = time.time()
        if now - self._day_start > 86400:
            self._day_start = now
            self._posted_today = 0

        if self._posted_today >= config.PARTNER_DAILY_LIMIT:
            return False

        min_interval = config.PARTNER_POST_INTERVAL_HOURS * 3600
        if now - self._last_post_time < min_interval:
            return False

        return True

    def mark_posted(self) -> None:
        """Mark that a partner post was just made."""
        self._last_post_time = time.time()
        self._posted_today += 1

    async def generate_partner_post_content(self, program: PartnerProgram) -> str:
        """Generate a natural-looking partner post for the channel."""
        cat_label = program.category_name or "авто"
        link = program.goto_link  # Use goto_link as-is!

        footer = f"Автор @asiaexp_bot\n@sochiautoparts\n#sochiautoparts"

        templates = [
            f"Рекомендую обратить внимание на {program.name} — отличный вариант, если речь идёт о {cat_label.lower()}. "
            f"Проверено, надёжный сервис. Загляните: {link}\n\n"
            f"{footer}",

            f"Для тех, кто ищет {cat_label.lower()}, советую {program.name}. "
            f"Удобный сервис, нормальные цены. Ссылка: {link}\n\n"
            f"{footer}",

            f"Часто спрашивают про {cat_label.lower()} — из проверенных вариантов отмечу {program.name}. "
            f"Сама проверяла, всё честно. Вот ссылка: {link}\n\n"
            f"{footer}",

            f"Если нужен {cat_label.lower()} — присмотритесь к {program.name}. "
            f"Хороший сервис, удобная навигация, адекватные цены: {link}\n\n"
            f"{footer}",
        ]

        return random.choice(templates)


# ── Global instance ────────────────────────────────────────────────────────────

partner_manager = PartnerManager()
