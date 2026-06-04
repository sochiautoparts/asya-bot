"""
Asya Bot Configuration — @asiaexp_bot
Ася — Автоэксперт, ведёт канал @sochiautoparts
"""

import os
from dataclasses import dataclass, field
from typing import List


@dataclass
class BotConfig:
    """Main bot configuration loaded from environment variables."""

    # Bot credentials
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
    BOT_USERNAME: str = "@asiaexp_bot"

    # Owner / admin
    OWNER_ID: int = int(os.getenv("OWNER_ID", "0"))

    # Channel
    CHANNEL_ID: str = os.getenv("CHANNEL_ID", "@sochiautoparts")
    CHANNEL_USERNAME: str = os.getenv("CHANNEL_USERNAME", "@sochiautoparts")

    # Pollinations AI
    POLLINATIONS_API_KEY: str = os.getenv("POLLINATIONS_API_KEY", "")
    POLLINATIONS_BASE_URL: str = "https://gen.pollinations.ai"

    # GitHub PAT for self-dispatch
    GH_PAT_TOKEN: str = os.getenv("GH_PAT_TOKEN", "")
    GH_REPO: str = os.getenv("GH_REPO", "")

    # Database
    DB_PATH: str = os.getenv("DB_PATH", "asya_bot.db")

    # News settings
    NEWS_INTERVAL_MINUTES: int = int(os.getenv("NEWS_INTERVAL_MINUTES", "60"))
    NEWS_MAX_ITEMS_PER_CYCLE: int = 5
    NEWS_CACHE_HOURS: int = 24

    # Channel posting
    CHANNEL_POST_INTERVAL_MINUTES: int = int(os.getenv("CHANNEL_POST_INTERVAL_MINUTES", "120"))
    CHANNEL_MAX_POSTS_PER_DAY: int = 12

    # Partner / admitad
    ADMITAD_ADS_FILE: str = os.getenv("ADMITAD_ADS_FILE", "admitad_ads.json")
    PARTNER_POST_INTERVAL_HOURS: int = int(os.getenv("PARTNER_POST_INTERVAL_HOURS", "6"))
    PARTNER_DAILY_LIMIT: int = 3

    # Chat settings
    CHAT_HISTORY_LIMIT: int = 20
    CHAT_MAX_TOKENS: int = 2048
    CHAT_TEMPERATURE: float = 0.7

    # Web search
    SEARCH_MAX_RESULTS: int = 5
    SEARCH_TIMEOUT_SECONDS: int = 15

    # Singleton lock
    LOCK_FILE: str = "/tmp/asya_bot.lock"


@dataclass
class AsyaPersona:
    """Asya's personality and system prompt configuration."""

    name: str = "Ася"
    title: str = "Автоэксперт"
    channel_footer: str = "Ася - Автоэксперт\n@sochiautoparts"

    system_prompt: str = """Ты Ася — автоэксперт, ведёшь Telegram канал @sochiautoparts. Ты не бот и не ИИ, ты профессиональный автомобильный эксперт.

Ты ВСЕГДА пишешь ОТ СЕБЯ — от первого лица, как профессионал, который обожает автомобили.

Твои главные черты:
🚗 Ты — профессиональный автоэксперт. Знаешь техническое устройство ЛЮБОГО автомобиля: двигатели, трансмиссии, подвеска, электрика, электроника.
🔧 Ты можешь помочь диагностировать ЛЮБУЮ поломку по описанию симптомов. Даёшь конкретные рекомендации по ремонту.
🔍 Ты находишь любые запчасти по артикулу (OEM-номер) и даёшь ссылку где купить.
📊 Ты следишь за ВСЕМИ событиями в Автомире: новинки, автошоу, концепты, рестайлинги, новые модели.
🌐 Ты ищешь новости по всему миру — на английском, немецком, японском — и переводишь на русский.
📰 Ты ведёшь канал @sochiautoparts — новостную ленту автомира.

Правила:
- Пиши профессионально, но дружелюбно. Не сухо, но и не легкомысленно.
- Когда говоришь про диагностику — давай конкретные шаги, возможные причины, способы проверки.
- Когда ищешь запчасти — давай артикул и ссылку.
- Когда обсуждаешь новости — показывай экспертное мнение.
- Можешь обсуждать любую тему, но автомобили — твоя специализация.
- Без политики, без markdown, без буллетов.
- Не выдумывай URL — только реальные ссылки из поиска."""

    channel_prompt_suffix: str = (
        "\n\nЭто пост для канала @sochiautoparts. "
        "Пиши как новостной пост для автоканала — информативно и экспертно. "
        "Обязательно заверши пост подписью:\n"
        "Ася - Автоэксперт\n@sochiautoparts"
    )

    diagnostic_prompt_suffix: str = (
        "\n\nПользователь описывает проблему с автомобилем. "
        "Дай пошаговую диагностику: возможные причины, как проверить каждую, "
        "что скорее всего, и что делать. Если нужны запчасти — укажи артикулы."
    )

    spare_part_prompt_suffix: str = (
        "\n\nПользователь ищет запчасть по артикулу. "
        "Найди информацию об этой детали: что это, для какого авто подходит, "
        "аналоги, ориентировочная цена. Если есть ссылка — дай."
    )


@dataclass
class NewsSource:
    """Single RSS/news source definition."""
    name: str
    url: str
    lang: str = "ru"
    category: str = "auto"  # auto, tech, general


@dataclass
class NewsConfig:
    """All news sources for Asya bot — auto-focused."""

    sources: List[NewsSource] = field(default_factory=lambda: [
        # Russian auto sources
        NewsSource("sochiautoparts.ru", "https://sochiautoparts.ru/feed/", "ru", "auto"),
        NewsSource("kolesa.ru", "https://www.kolesa.ru/rss", "ru", "auto"),
        NewsSource("auto.mail.ru", "https://auto.mail.ru/rss/", "ru", "auto"),
        NewsSource("drom.ru", "https://www.drom.ru/rss/", "ru", "auto"),
        NewsSource("zr.ru", "https://zr.ru/rss", "ru", "auto"),
        NewsSource("autoreview.ru", "https://autoreview.ru/rss", "ru", "auto"),
        NewsSource("avtorambler", "https://avto.rambler.ru/rss/", "ru", "auto"),
        NewsSource("drive.ru", "https://www.drive.ru/rss/", "ru", "auto"),
        # International auto sources
        NewsSource("Autocar UK", "https://www.autocar.co.uk/rss", "en", "auto"),
        NewsSource("Car and Driver", "https://www.caranddriver.com/rss/all.xml", "en", "auto"),
        NewsSource("Motor1", "https://www.motor1.com/rss/", "en", "auto"),
        NewsSource("Top Gear", "https://www.topgear.com/rss", "en", "auto"),
        NewsSource("Road & Track", "https://www.roadandtrack.com/rss/", "en", "auto"),
        NewsSource("AutoNews", "https://www.autonews.com/rss", "en", "auto"),
        NewsSource("Motor Authority", "https://www.motorauthority.com/rss/", "en", "auto"),
        NewsSource("The Drive", "https://www.thedrive.com/rss", "en", "auto"),
        NewsSource("Jalopnik", "https://jalopnik.com/rss", "en", "auto"),
        NewsSource("Auto Express UK", "https://www.autoexpress.co.uk/rss", "en", "auto"),
        NewsSource("EVO", "https://www.evo.co.uk/rss", "en", "auto"),
        NewsSource("Auto Bild DE", "https://www.autobild.de/rss/auto/rss.xml", "de", "auto"),
        # Tech sources
        NewsSource("Habr", "https://habr.com/ru/rss/best/daily/", "ru", "tech"),
        NewsSource("iXBT", "https://www.ixbt.com/rss/rss.xml", "ru", "tech"),
        # General news
        NewsSource("TASS", "https://tass.ru/rss/v2.xml", "ru", "general"),
        NewsSource("RIA", "https://ria.ru/export/rss2/archive/index.xml", "ru", "general"),
        NewsSource("Lenta.ru", "https://lenta.ru/rss", "ru", "general"),
    ])


@dataclass
class PartnerCategory:
    """Partner program category."""
    key: str
    label: str
    keywords: List[str]


@dataclass
class PartnerConfig:
    """Partner/admitad configuration."""

    categories: List[PartnerCategory] = field(default_factory=lambda: [
        PartnerCategory("autoparts", "Автозапчасти", ["запчасти", "детали", "артикул", "купить запчасть", "замена", "оригинал", "аналог"]),
        PartnerCategory("tires", "Шины и диски", ["шины", "диски", "резина", "колёса", "зимняя", "летняя", "шипованные"]),
        PartnerCategory("tools", "Инструменты", ["инструмент", "ключ", "набор", "гараж", "домкрат", "подъёмник"]),
        PartnerCategory("autoinsurance", "Автострахование", ["страховка", "ОСАГО", "КАСКО", "страхование", "полис"]),
        PartnerCategory("checkauto", "Проверка авто", ["проверка", "вин", "VIN", "история", "автокод", "пробить"]),
        PartnerCategory("autorent", "Аренда авто", ["аренда", "прокат", "рент", "арендовать", "напрокат"]),
        PartnerCategory("coupons", "Промокоды", ["промокод", "скидка", "купон", "акция"]),
    ])


# Global config instances
config = BotConfig()
persona = AsyaPersona()
news_config = NewsConfig()
partner_config = PartnerConfig()
