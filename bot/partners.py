"""
Partner Program Integration v4.0

Loads partner data from remote partners.json (sochiautoparts.ru — updateable file!).
Uses goto_link EXACTLY as-is — no subid additions, no modifications.
The goto_links are ready for both posts and user dialogs.

Source: https://sochiautoparts.ru/partners.json
Schema:
    {
      "updated": "<iso-ts>",
      "campaigns": [
        {
          "id": 38740,
          "name": "Autopiter KZ",
          "logo": "https://cdn.admitad-connect.com/...",
          "goto_link": "https://xmknb.com/g/.../",
          "site_url": "https://autopiter.kz/",
          "regions": ["KZ"],
          "categories": ["Интернет-магазины", "Автомобили и мотоциклы", ...]
        },
        ...
      ]
    }

Key features:
- Downloads partners.json from sochiautoparts.ru (updateable file!)
- Auto-refreshes every 6 hours
- Uses goto_link EXACTLY as provided — NO subid additions
- Photo/logo extracted from the "logo" field
- regions  -> allowed_regions (regional filtering)
- categories (RU strings) -> internal category key + human-readable label
  (autoparts / tires / tools / autoinsurance / checkauto / autorent / coupons / other)
- For article searches, adds a ulp parameter to admitad /g/{hash}/ shortlinks
  so the user lands on the search page while affiliate tracking is preserved.
- Proper formatting: "Name (category description): goto_link"

Backward compatibility: the legacy admitad_ads.json schema (allowed_regions,
category, category_name, image, ...) is still accepted, so old cache files
keep working.
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

# Remote partners.json URL — sochiautoparts.ru (updateable file!)
PARTNERS_JSON_URL = "https://sochiautoparts.ru/partners.json"
PARTNERS_LOCAL_CACHE = "data/partners.json"
PARTNERS_REFRESH_INTERVAL = 6 * 3600  # Refresh every 6 hours

# Backward-compatible aliases (legacy code may still reference these names)
ADMITAD_JSON_URL = PARTNERS_JSON_URL
ADMITAD_LOCAL_CACHE = PARTNERS_LOCAL_CACHE
ADMITAD_REFRESH_INTERVAL = PARTNERS_REFRESH_INTERVAL

# Default region for partner filtering
DEFAULT_REGION = "RU"


class PartnerProgram:
    """Single partner program from partners.json (sochiautoparts.ru).

    Accepts BOTH the new partners.json schema (regions, categories, logo)
    and the legacy admitad_ads.json schema (allowed_regions, category, ...).
    """

    # ── Category derivation tables ────────────────────────────────────────
    # Internal category keys used across the bot:
    #   autoparts, tires, tools, autoinsurance, checkauto, autorent, coupons, other
    #
    # The new partners.json exposes human-readable Russian category strings
    # (e.g. "Товары для авто и мотоциклов", "Аренда машин"). We map each
    # campaign to an internal key using its site_url domain (most reliable)
    # with a fallback to the Russian category strings.

    # site_url/name substring → internal category key (order matters: most
    # specific first, e.g. tyres are checked before generic autoparts).
    _SITE_CATEGORY_MAP: List[Tuple[str, str]] = [
        # Tires & wheels (checked first — they also carry "Товары для авто")
        ("euro-diski.ru", "tires"),
        ("bs-tyres.ru", "tires"),
        ("koleso.ru", "tires"),
        # Check auto
        ("avtocod.ru", "checkauto"),
        # Insurance / fuel cards
        ("petrolplus.ru", "autoinsurance"),
        # Car rental
        ("discovercars.com", "autorent"),
        ("localrent.com", "autorent"),
        # Auto parts & auto goods
        ("rossko.ru", "autoparts"),
        ("autopiter.ru", "autoparts"),
        ("autopiter.kz", "autoparts"),
        ("avtoall.ru", "autoparts"),
        ("globaldrive.ru", "autoparts"),
        ("mirdvornikov.ru", "autoparts"),
        ("hyperauto.ru", "autoparts"),
        ("lukoil-shop", "autoparts"),
        # Marketplaces / coupons
        ("aliexpress", "coupons"),
        ("alibaba", "coupons"),
        ("geekbuying", "coupons"),
        ("raketacn.ru", "coupons"),
        ("raketa", "coupons"),
    ]

    # internal category key → human-readable label (RU)
    _CATEGORY_LABELS: Dict[str, str] = {
        "autoparts": "Автозапчасти",
        "tires": "Шины и диски",
        "tools": "Автоинструменты",
        "autoinsurance": "Автострахование",
        "checkauto": "Проверка авто",
        "autorent": "Аренда авто",
        "coupons": "Маркетплейсы и скидки",
        "other": "Полезный сервис",
    }

    def __init__(self, data: Dict):
        self.id = str(data.get("id", ""))
        self.name = data.get("name", "")
        self.slug = data.get("slug", "")
        # Photo / logo — new source uses "logo", legacy used image/image_url
        self.image = (
            data.get("logo") or
            data.get("image") or
            data.get("image_url") or
            data.get("brand_logo") or
            ""
        )
        self.description = data.get("description", "")
        self.ad_text = data.get("ad_text", "")
        self.goto_link = data.get("goto_link", "")
        self.site_url = data.get("site_url", "")
        # Regions — new source uses "regions", legacy used "allowed_regions"
        self.allowed_regions = (
            data.get("regions") or
            data.get("allowed_regions") or
            []
        )
        self.rating = data.get("rating", "")
        # Categories — new source uses "categories" (list of RU strings)
        self.categories_list = data.get("categories", []) or []
        # Derive internal category key + human-readable category_name
        self.category, self.category_name = self._derive_category(data)
        self.raw = data

    def _derive_category(self, data: Dict) -> Tuple[str, str]:
        """Derive (internal_category_key, human_readable_label) for this campaign.

        Priority:
        1. Explicit legacy fields (category / category_name) if present.
        2. site_url / name match against _SITE_CATEGORY_MAP (most reliable).
        3. Russian category string heuristics.
        4. Fallback to ("other", first RU category or "Полезный сервис").
        """
        # 1. Legacy explicit fields (old admitad_ads.json)
        legacy_cat = data.get("category", "")
        legacy_cat_name = data.get("category_name", "")
        if legacy_cat:
            label = legacy_cat_name or self._CATEGORY_LABELS.get(
                legacy_cat, legacy_cat_name or self._CATEGORY_LABELS["other"]
            )
            return legacy_cat, label

        site_lower = (self.site_url or "").lower()
        name_lower = (self.name or "").lower()
        cats_joined = " | ".join(self.categories_list).lower()

        # 2. site_url / name domain match
        for domain_fragment, cat_key in self._SITE_CATEGORY_MAP:
            if domain_fragment in site_lower or domain_fragment in name_lower:
                return cat_key, self._CATEGORY_LABELS[cat_key]

        # 3. Russian category string heuristics
        if "аренда машин" in cats_joined:
            return "autorent", self._CATEGORY_LABELS["autorent"]
        if "автострах" in cats_joined or "страхование" in cats_joined:
            return "autoinsurance", self._CATEGORY_LABELS["autoinsurance"]
        if any(k in cats_joined for k in ("шины", "диски", "колёса", "колеса")):
            return "tires", self._CATEGORY_LABELS["tires"]
        if "товары для авто" in cats_joined or "автомобили и мотоциклы" in cats_joined:
            # Could be parts, tyres, or tools — default to autoparts
            return "autoparts", self._CATEGORY_LABELS["autoparts"]
        if "маркетплейс" in cats_joined:
            return "coupons", self._CATEGORY_LABELS["coupons"]

        # 4. Fallback
        other_label = (
            self.categories_list[0] if self.categories_list
            else self._CATEGORY_LABELS["other"]
        )
        return "other", other_label

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

        The affiliate tracking part of goto_link is ALWAYS preserved.

        - If goto_link already has a ulp parameter → replace it so the user
          lands on the search results page on the partner's site.
        - If goto_link is an admitad /g/{hash}/ shortlink WITHOUT ulp → ADD a
          ulp parameter (admitad shortlinks support ulp) so the user lands on
          the search page while affiliate tracking is still applied.
        - Otherwise (e.g. ali.click style links, or unknown site) → return
          goto_link EXACTLY as-is.
        """
        if not self.goto_link:
            return ""
        if not query:
            return self.goto_link

        try:
            parsed = urlparse(self.goto_link)
            params = parse_qs(parsed.query)

            # Build the search URL on the partner's own site (raw, unencoded)
            search_url = self._build_search_url(self.site_url, query)
            # _build_search_url returns the original input unchanged when no
            # site-specific pattern is known.
            if not search_url or search_url == self.site_url:
                # No site-specific search pattern — use goto_link as-is
                return self.goto_link

            if "ulp" in params and params["ulp"]:
                # Existing ulp → replace it
                original_ulp = params["ulp"][0]
                if search_url != original_ulp:
                    new_params = {}
                    for k, v_list in params.items():
                        if k == "ulp":
                            new_params[k] = search_url
                        else:
                            new_params[k] = v_list[0] if len(v_list) == 1 else v_list
                    new_query = urlencode(new_params, doseq=True)
                    return urlunparse(parsed._replace(query=new_query))
                return self.goto_link

            # No ulp present. For admitad /g/{hash}/ shortlinks we can ADD a
            # ulp parameter to deep-link to the search page (tracking kept).
            path = parsed.path or ""
            is_admitad_shortlink = "/g/" in path
            if is_admitad_shortlink:
                merged = {k: (v[0] if len(v) == 1 else v) for k, v in params.items()}
                merged["ulp"] = search_url  # urlencode will URL-encode the value
                new_query = urlencode(merged, doseq=True)
                return urlunparse(parsed._replace(query=new_query))

        except Exception as e:
            logger.debug(f"Error modifying goto_link for search: {e}")

        # If we can't modify, return as-is
        return self.goto_link

    def _build_search_url(self, original_ulp: str, query: str) -> str:
        """Build a raw (unencoded) search URL on the partner's site.

        Returns the search URL string, or `original_ulp` unchanged if no
        site-specific search pattern is known. The caller is responsible for
        URL-encoding the result when placing it into a query parameter
        (urlencode handles that).
        """
        site_url = (self.site_url or "").rstrip("/")
        if not site_url:
            return original_ulp
        query_encoded = quote_plus(query)

        # Site-specific search URL patterns (raw URLs — urlencode encodes later)
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
            "lukoil-shop": f"{site_url}/search/?q={query_encoded}",
        }

        # Check if site_url matches any pattern
        for domain, pattern in search_patterns.items():
            if domain in (self.site_url or ""):
                return pattern  # raw URL — urlencode encodes it later

        # Generic fallback: no known pattern
        return original_ulp

    def format_link(self, with_description: bool = True) -> str:
        """Format this partner's link for display.

        Uses goto_link EXACTLY as-is from the source.
        No subid additions — the link is ready to use!
        """
        if not self.goto_link:
            return ""

        if with_description and self.category_name:
            return f"{self.name} ({self.category_name}): {self.goto_link}"
        return f"{self.name}: {self.goto_link}"

    def format_link_with_search(self, query: str) -> str:
        """Format this partner's link with a search query.

        Adds/replaces the ulp parameter in goto_link to point at a search
        results page. The base goto_link (with affiliate tracking) is preserved.
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

    v4.0: Downloads partners.json from sochiautoparts.ru, auto-refreshes.
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

    # Alias for backward compatibility
    load_admitad_async = load_async

    async def _load_from_remote(self) -> int:
        """Download partners.json from sochiautoparts.ru."""
        try:
            import httpx
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                response = await client.get(PARTNERS_JSON_URL)
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
            logger.warning(f"Failed to load partners.json from remote: {e}")
        return 0

    def _load_from_local(self) -> int:
        """Load from local cache file."""
        # Try data/partners.json first, then root, then legacy filenames
        for filepath in [
            PARTNERS_LOCAL_CACHE,
            "partners.json",
            "data/admitad_ads.json",
            "admitad_ads.json",
        ]:
            path = Path(filepath)
            if path.exists():
                try:
                    content = path.read_text(encoding="utf-8").strip()
                    if not content.startswith(("{", "[")):
                        logger.warning(f"Local partner cache is not JSON (starts with '{content[:20]}...'), removing")
                        path.unlink(missing_ok=True)
                        continue
                    data = json.loads(content)
                    count = self._parse_programs(data)
                    self._loaded = True
                    self._last_load_time = time.time()
                    logger.info(f"Loaded {count} partner programs from local cache: {filepath}")
                    return count
                except json.JSONDecodeError as e:
                    logger.error(f"Invalid JSON in local partner cache ({filepath}): {e}")
                    # Remove corrupted cache file
                    try:
                        path.unlink(missing_ok=True)
                    except Exception:
                        pass
                except Exception as e:
                    logger.error(f"Error loading local partner cache: {e}")
        logger.warning("No partners.json found locally or remotely")
        self._loaded = True
        return 0

    def load(self, filepath: str = "") -> int:
        """Synchronous load from local file only."""
        filepath = filepath or config.ADMITAD_ADS_FILE
        path = Path(filepath)
        if not path.exists():
            path = Path(PARTNERS_LOCAL_CACHE)
        if not path.exists():
            path = Path("partners.json")

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
        """Parse programs from JSON data.

        Supports:
        - New partners.json: { "campaigns": [ ... ] }
        - Legacy admitad_ads.json: list, or { "programs"|"items"|"results": [...] }
        """
        self.programs = []
        self._site_map = {}

        items = []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = (
                data.get("campaigns") or
                data.get("programs") or
                data.get("items") or
                data.get("results") or
                []
            )
            if not isinstance(items, list):
                items = []

        for item in items:
            prog = PartnerProgram(item)
            if prog.goto_link:
                self.programs.append(prog)
                # Build site URL mapping (domain → program) for fast lookup
                if prog.site_url:
                    domain = urlparse(prog.site_url).netloc.replace("www.", "")
                    if domain:
                        self._site_map[domain] = prog

        return len(self.programs)

    def _save_cache(self, data) -> None:
        """Save data to local cache."""
        try:
            cache_path = Path(PARTNERS_LOCAL_CACHE)
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"Failed to save partner cache: {e}")

    def ensure_loaded(self) -> None:
        """Load partner programs if not yet loaded."""
        if not self._loaded:
            self.load()

    async def maybe_refresh(self) -> None:
        """Refresh from remote if enough time has passed."""
        if not self._loaded or (time.time() - self._last_load_time > PARTNERS_REFRESH_INTERVAL):
            await self.load_async()

    def get_by_category(self, category: str, region: str = DEFAULT_REGION) -> List[PartnerProgram]:
        """Get programs in a specific category and region."""
        self.ensure_loaded()
        return [p for p in self.programs if p.has_category(category) and p.has_region(region)]

    def get_by_site(self, site_url: str) -> Optional[PartnerProgram]:
        """Find a partner program by its site URL or domain."""
        self.ensure_loaded()
        if not site_url:
            return None

        # Try parsing as URL first
        domain = urlparse(site_url).netloc.replace("www.", "") if site_url else ""

        # If urlparse didn't extract a netloc (bare domain like "rossko.ru"),
        # treat the input itself as the domain
        if not domain and site_url:
            domain = site_url.replace("www.", "").rstrip("/")

        # Direct lookup
        result = self._site_map.get(domain)
        if result:
            return result

        # Fallback: partial match on domain keys
        for key, prog in self._site_map.items():
            if domain in key or key in domain:
                return prog

        return None

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

        v4: Uses goto_link from partners.json EXACTLY as-is.
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
        lines.append("ВАЖНО: Ссылки выше — ПАРТНЁРСКИЕ (goto_link из partners.json). Используй их КАК ЕСТЬ, ничего не добавляй и не меняй!")

        return "\n".join(lines)

    def get_primary_parts_links(self, region: str = DEFAULT_REGION) -> List[Dict[str, str]]:
        """Get the THREE primary partner links for auto parts in strict order.

        Order: 1) Rossko, 2) Autopiter (RU), 3) AvtoALL
        These are the main links Ася gives in EVERY parts/VIN query.
        """
        self.ensure_loaded()
        links = []

        # Define the strict order: Rossko → Autopiter RU → AvtoALL
        primary_sites = [
            ("rossko.ru", "Росско", "профессиональный подбор запчастей"),
            ("autopiter.ru", "Autopiter", "крупнейший магазин автозапчастей в России"),
            ("avtoall.ru", "AvtoALL", "автотовары и запчасти"),
        ]

        for site_domain, display_name, description in primary_sites:
            prog = self.get_by_site(site_domain)
            if prog and prog.has_region(region):
                links.append({
                    "name": display_name,
                    "url": prog.goto_link,
                    "description": description,
                })
            else:
                # Fallback: search by category
                logger.debug(f"Primary partner {site_domain} not found in loaded programs")

        return links

    def format_primary_parts_links(self, region: str = DEFAULT_REGION) -> str:
        """Format the three primary partner links as context for AI.

        Returns a string like:
        Партнёрские ссылки для запчастей (используй КАК ЕСТЬ!):
        1. Росско (профессиональный подбор запчастей): https://...
        2. Autopiter (крупнейший магазин автозапчастей в России): https://...
        3. AvtoALL (автотовары и запчасти): https://...
        """
        links = self.get_primary_parts_links(region)
        if not links:
            return ""

        lines = [
            "ПАРТНЁРСКИЕ ССЫЛКИ ДЛЯ ЗАПЧАСТЕЙ (давай ВСЕГДА в этом порядке! Используй КАК ЕСТЬ, ничего не меняй!):",
        ]
        for i, link in enumerate(links, 1):
            lines.append(f"{i}. {link['name']} ({link['description']}): {link['url']}")
        lines.append("")
        lines.append("На всех трёх сайтах можно искать по VIN-коду и артикулу запчастей. Есть чаты с подбором запчастей.")

        return "\n".join(lines)

    def get_all_partner_links_for_parts(self, query: str, region: str = DEFAULT_REGION) -> List[Dict[str, str]]:
        """Get all partner links relevant to a parts query.

        Returns list of dicts with 'name', 'url', 'description' keys.
        Uses goto_link from partners.json.
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

    def get_all_relevant_links(self, text: str, max_programs: int = 5, region: str = DEFAULT_REGION) -> List[Dict[str, str]]:
        """Get ALL relevant partner links across ALL categories for a given text.

        Unlike get_primary_parts_links() which only returns autoparts, this method
        detects ALL relevant categories (autoparts, tires, tools, insurance, checkauto, etc.)
        and returns links from ALL matching categories.

        For car-related queries, includes: autoparts + tires + tools + insurance + checkauto.
        For general queries, includes relevant categories based on keywords.

        Returns list of dicts with 'name', 'url', 'description' keys.
        """
        self.ensure_loaded()
        links = []
        seen_names = set()
        text_lower = text.lower()

        # Detect ALL relevant categories based on keywords
        relevant_categories = set()

        # Always include autoparts for car-related queries
        auto_keywords = [
            "запчаст", "деталь", "артикул", "купить запчас", "купить детал",
            "оригинал", "аналог", "замена", "подбор", "номер детал",
            "oem", "оригинальн", "поиск запчас", "найти запчас",
            "фильтр", "колодки", "свечи", "ремень", "прокладк",
            "сальник", "подшипник", "амортизатор", "реле", "датчик",
            "масло", "антифриз", "тормозн", "где купить",
            "росско", "rossko", "autopiter", "автопитер",
            "vin", "вин", "машина", "машин", "авто", "мотор", "двигатель",
            "ремонт", "поломк", "стучит", "диагност",
            "avtoall", "exist", "emex", "autodoc",
        ]
        tire_keywords = [
            "шины", "диски", "резина", "колёса", "зимняя", "летняя",
            "шипованные", "шиповк", "покрышк", "euro-diski", "bs-tyres",
            "сезонная смен", "переобув",
        ]
        tools_keywords = [
            "инструмент", "ключ", "набор", "гараж", "домкрат",
            "avtoall", "подъёмник", "станок",
        ]
        insurance_keywords = [
            "страховка", "осаго", "каско", "страхование", "полис",
            "petrolplus", "автострахов",
        ]
        checkauto_keywords = [
            "проверка", "вин", "vin", "история", "автокод", "пробить",
            "hyperauto", "проверить авто", "история автомобил",
        ]
        rent_keywords = [
            "аренда", "прокат", "рент", "арендовать", "напрокат",
            "discovercars", "localrent",
        ]

        if any(kw in text_lower for kw in auto_keywords):
            relevant_categories.add("autoparts")
        if any(kw in text_lower for kw in tire_keywords):
            relevant_categories.add("tires")
        if any(kw in text_lower for kw in tools_keywords):
            relevant_categories.add("tools")
        if any(kw in text_lower for kw in insurance_keywords):
            relevant_categories.add("autoinsurance")
        if any(kw in text_lower for kw in checkauto_keywords):
            relevant_categories.add("checkauto")
        if any(kw in text_lower for kw in rent_keywords):
            relevant_categories.add("autorent")

        # If no specific category detected, default to autoparts for car queries
        if not relevant_categories:
            # Check if it's a car-related query at all
            car_kw = ["авто", "машина", "машин", "двигатель", "мотор", "car", "auto",
                      "кузов", "ходов", "подвеск", "тормоз", "руль", "коробк"]
            if any(kw in text_lower for kw in car_kw):
                relevant_categories.add("autoparts")
                # For car queries, also include these common cross-categories
                relevant_categories.add("tires")
                relevant_categories.add("tools")
                relevant_categories.add("checkauto")

        # Collect programs from all relevant categories
        for cat in relevant_categories:
            cat_programs = self.get_by_category(cat, region)
            for p in cat_programs:
                if p.name not in seen_names and p.goto_link:
                    seen_names.add(p.name)
                    desc = p._get_category_description()
                    links.append({
                        "name": p.name,
                        "url": p.goto_link,
                        "description": f"{p.category_name} — {desc}",
                    })

        # Always ensure primary autoparts links are included if autoparts is relevant
        if "autoparts" in relevant_categories:
            primary_sites = ["rossko.ru", "autopiter.ru", "avtoall.ru"]
            for site in primary_sites:
                prog = self.get_by_site(site)
                if prog and prog.name not in seen_names and prog.goto_link:
                    seen_names.add(prog.name)
                    desc = prog._get_category_description()
                    links.append({
                        "name": prog.name,
                        "url": prog.goto_link,
                        "description": f"{prog.category_name} — {desc}",
                    })

        return links[:max_programs]

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
