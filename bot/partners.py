"""
Admitad Partner Program Integration
Loads partner data from admitad_ads.json, inserts affiliate links naturally,
posts partner content to channel on schedule.
"""

import json
import random
import time
import logging
from pathlib import Path
from typing import List, Dict, Optional, Tuple

from bot.config import config, partner_config

logger = logging.getLogger("asya.partners")


class PartnerProgram:
    """Single partner program from admitad."""

    def __init__(self, data: Dict):
        self.id = str(data.get("id", ""))
        self.name = data.get("name", "")
        self.slug = data.get("slug", "")
        self.image = data.get("image", "") or data.get("image_url", "") or data.get("logo", "") or data.get("brand_logo", "")
        self.description = data.get("description", "")
        self.goto_link = data.get("goto_link", "")
        self.categories = self._extract_categories(data)
        self.regions = self._extract_regions(data)
        self.raw = data

    def _extract_categories(self, data: Dict) -> List[str]:
        """Extract categories from program data."""
        cats = []
        # Check direct category field
        if "category" in data:
            cat = data["category"]
            if isinstance(cat, str):
                cats.append(cat)
            elif isinstance(cat, list):
                cats.extend(cat)
            elif isinstance(cat, dict):
                cats.append(cat.get("slug", cat.get("name", "")))
        # Check categories list
        if "categories" in data:
            for c in data["categories"]:
                if isinstance(c, str):
                    cats.append(c)
                elif isinstance(c, dict):
                    cats.append(c.get("slug", c.get("name", "")))
        return [c.lower().strip() for c in cats if c]

    def _extract_regions(self, data: Dict) -> List[str]:
        """Extract region info."""
        regions = []
        if "region_groups" in data:
            rg = data["region_groups"]
            if isinstance(rg, list):
                for r in rg:
                    if isinstance(r, dict):
                        regions.append(r.get("region", r.get("country", "")))
                    elif isinstance(r, str):
                        regions.append(r)
            elif isinstance(rg, dict):
                regions.extend(rg.keys())
        if "regions" in data:
            r = data["regions"]
            if isinstance(r, list):
                regions.extend(r)
        return [reg.lower() for reg in regions if reg]

    def has_region(self, region: str = "ru") -> bool:
        """Check if program is available in a region."""
        if not self.regions:
            return True  # No region info = assume available everywhere
        return region.lower() in self.regions or any(region.lower() in r for r in self.regions)

    def has_category(self, category: str) -> bool:
        """Check if program belongs to a category."""
        cat_lower = category.lower()
        return cat_lower in self.categories

    def matches_keywords(self, text: str) -> bool:
        """Check if text contains keywords for this program's categories."""
        text_lower = text.lower()
        for cat in self.categories:
            for pc in partner_config.categories:
                if pc.key.lower() == cat.lower():
                    for kw in pc.keywords:
                        if kw.lower() in text_lower:
                            return True
        return False


class PartnerManager:
    """Manages all partner programs — loading, matching, posting."""

    def __init__(self):
        self.programs: List[PartnerProgram] = []
        self._loaded = False
        self._last_post_time: float = 0
        self._posted_today: int = 0
        self._day_start: float = 0

    def load(self, filepath: str = "") -> int:
        """Load partner programs from JSON file. Returns count loaded."""
        filepath = filepath or config.ADMITAD_ADS_FILE
        path = Path(filepath)

        if not path.exists():
            logger.warning(f"Partner ads file not found: {filepath}")
            self._loaded = True
            return 0

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)

            self.programs = []

            if isinstance(data, list):
                for item in data:
                    prog = PartnerProgram(item)
                    if prog.goto_link:
                        self.programs.append(prog)
            elif isinstance(data, dict):
                # Could be nested under "programs", "items", etc.
                items = data.get("programs", data.get("items", data.get("results", [])))
                if isinstance(items, list):
                    for item in items:
                        prog = PartnerProgram(item)
                        if prog.goto_link:
                            self.programs.append(prog)
                # Also check for region_groups with sub-programs
                for item in data.get("region_groups", []):
                    if isinstance(item, dict):
                        for sub in item.get("programs", item.get("items", [])):
                            prog = PartnerProgram(sub)
                            if prog.goto_link:
                                self.programs.append(prog)

            self._loaded = True
            logger.info(f"Loaded {len(self.programs)} partner programs")
            return len(self.programs)

        except Exception as e:
            logger.error(f"Error loading partner ads: {e}")
            self._loaded = True
            return 0

    def ensure_loaded(self) -> None:
        """Load partner programs if not yet loaded."""
        if not self._loaded:
            self.load()

    def get_by_category(self, category: str, region: str = "ru") -> List[PartnerProgram]:
        """Get programs in a specific category and region."""
        self.ensure_loaded()
        return [p for p in self.programs if p.has_category(category) and p.has_region(region)]

    def get_all_categories(self) -> List[str]:
        """Get all available categories across programs."""
        self.ensure_loaded()
        cats = set()
        for p in self.programs:
            cats.update(p.categories)
        return sorted(cats)

    def find_matching_programs(self, text: str, region: str = "ru") -> List[PartnerProgram]:
        """Find programs that match keywords in the text."""
        self.ensure_loaded()
        matches = []
        for p in self.programs:
            if p.has_region(region) and p.matches_keywords(text):
                matches.append(p)
        return matches

    def get_random_program(self, category: str = "", region: str = "ru") -> Optional[PartnerProgram]:
        """Get a random partner program, optionally filtered by category."""
        self.ensure_loaded()
        pool = self.get_by_category(category, region) if category else [p for p in self.programs if p.has_region(region)]
        return random.choice(pool) if pool else None

    def format_affiliate_link(self, program: PartnerProgram, context: str = "") -> str:
        """Format an affiliate link for a program."""
        url = program.goto_link
        # Add subid for tracking if possible
        if "?" in url:
            url += f"&subid=asya_bot"
        else:
            url += f"?subid=asya_bot"
        return url

    def generate_partner_context(self, text: str, max_programs: int = 2) -> str:
        """
        Generate context about matching partner programs for AI to reference
        naturally in its response.
        """
        self.ensure_loaded()
        if not self.programs:
            return ""

        matches = self.find_matching_programs(text)
        if not matches:
            # Try a broader match by category keywords
            for pc in partner_config.categories:
                if any(kw in text.lower() for kw in pc.keywords):
                    cat_programs = self.get_by_category(pc.key)
                    if cat_programs:
                        matches = cat_programs[:max_programs]
                        break

        if not matches:
            return ""

        lines = ["Релевантные партнёрские программы (можно упомянуть естественно в ответе):"]
        for p in matches[:max_programs]:
            link = self.format_affiliate_link(p)
            cat_str = ", ".join(p.categories[:3]) if p.categories else "общая"
            lines.append(f"- {p.name} (категория: {cat_str}) — ссылка: {link}")
            if p.description:
                lines.append(f"  Описание: {p.description[:150]}")

        return "\n".join(lines)

    def should_post_partner(self) -> bool:
        """Check if it's time to post a partner message to channel."""
        now = time.time()
        # Reset daily counter
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
        cat_label = ""
        for pc in partner_config.categories:
            if program.has_category(pc.key):
                cat_label = pc.label
                break

        link = self.format_affiliate_link(program)

        footer = f"[Ася - Автоэксперт](https://t.me/asiaexp_bot)\n@sochiautoparts\n#sochiautoparts"

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
