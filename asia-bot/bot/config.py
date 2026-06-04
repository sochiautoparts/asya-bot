"""Asya Bot 1.0 — AUTO EXPERT + NEWS HUNTER + PARTNER INTEGRATION!

Ася — автоэксперт, ведёт канал @sochiautoparts:
  - Ищет в интернете 24/7 свежие автоновости на любых языках, переводит на русский
  - Подбирает фото из новостей или генерирует для постов
  - Следит за всеми событиями в Автомире: новинки, автошоу, автозапчасти
  - В каждом посте: Ася — Автоэксперт, потом @sochiautoparts
  - Диагностирует поломки, находит запчасти по артикулу с ссылкой
  - Партнёрские программы от admitad — естественная интеграция в контент
  - Знает текущую дату, живёт по московскому времени
"""
import os
from typing import Dict, List


def _env(name: str, default: str = "") -> str:
    """Get env var, treating 'not_configured' and empty as default."""
    val = os.environ.get(name, default)
    if val in ("not_configured", "NOT_CONFIGURED", ""):
        return default
    return val


def _env_int(name: str, default: int = 0) -> int:
    """Get env var as int, with fallback default."""
    val = _env(name)
    try:
        return int(val) if val else default
    except ValueError:
        return default


# ── Bot Core ────────────────────────────────────────────────
BOT_TOKEN: str = _env("BOT_TOKEN", "8786860829:AAEHco-Irc0szHYeX-p9j5vcQ0m5gaXrz3E")

# ── Pollinations.ai — MULTI-MODEL AI Provider ─────────────────
POLLINATIONS_API_KEY: str = _env("POLLINATIONS_API_KEY", "")
# Models pool (configured in pollinations_provider.py)
POLLINATIONS_TIMEOUT: float = 45.0
POLLINATIONS_MAX_TOKENS: int = 2000  # Full detailed responses — no limits!
POLLINATIONS_MAX_RETRIES: int = 3  # Try up to 3 models on failure

# ── Local Model Toggle ──────────────────────────────────────
# Set ENABLE_LOCAL_MODEL=true to load Qwen3-4B as local fallback
# Default: disabled (cloud-only mode — faster startup, less RAM)
ENABLE_LOCAL_MODEL: bool = _env("ENABLE_LOCAL_MODEL", "false").lower() in ("true", "1", "yes")

# ── LlamaCpp Model — LOCAL FALLBACK (disabled by default!) ────
# Qwen3-4B-Instruct — only when ALL Pollinations models are unavailable
# Only loaded when ENABLE_LOCAL_MODEL=true
MODEL_PATH: str = _env("MODEL_PATH", "models/Qwen3-4B-Instruct-2507-Q4_K_M.gguf") if ENABLE_LOCAL_MODEL else ""

MODEL_N_CTX: int = _env_int("MODEL_N_CTX", 2048)
MODEL_N_THREADS: int = _env_int("MODEL_N_THREADS", 4)
MODEL_MAX_TOKENS: int = _env_int("MODEL_MAX_TOKENS", 256)
MODEL_HISTORY_LIMIT: int = _env_int("MODEL_HISTORY_LIMIT", 10)

OWNER_ID: int = _env_int("OWNER_ID", 0)
ADMIN_IDS: List[int] = list(set(
    [OWNER_ID] + [int(x) for x in _env("ADMIN_IDS", str(OWNER_ID) if OWNER_ID else "").split(",") if x.strip().isdigit()]
))
BOT_USERNAME: str = _env("BOT_USERNAME", "asiaexp_bot")

# ── Server ─────────────────────────────────────────────────
API_HOST: str = _env("API_HOST", "0.0.0.0")
API_PORT: int = _env_int("API_PORT", 8082)
DB_PATH: str = _env("DB_PATH", "data/asya.db")
SESSION_DURATION_SECONDS = 20700
LOG_LEVEL: str = _env("LOG_LEVEL", "INFO")

# ── AI Cache Settings ──────────────────────────────────────
CACHE_TTL_TEXT = 3600
CACHE_MAX_MEMORY = 500

# ── Telegram Channel ──────────────────────────────────────
CHANNEL_ID: str = _env("CHANNEL_ID")
CHANNEL_USERNAME: str = _env("CHANNEL_USERNAME", "sochiautoparts")

# ── Timezone ──────────────────────────────────────────────
MOSCOW_TZ = "Europe/Moscow"

# ── Admitad Partner Integration ──────────────────────────────
ADMITAD_ADS_FILE: str = _env("ADMITAD_ADS_FILE", "admitad_ads.json")
ADMITAD_CHECK_INTERVAL: int = _env_int("ADMITAD_CHECK_INTERVAL", 3600)  # Refresh every hour
ADMITAD_MIN_RELEVANCE: float = 0.6  # Minimum relevance score for partner link suggestion

# ── Partner Link Format ──────────────────────────────────────
# Partner links in posts: [Brand–Category](URL)
# Examples: [Роско–Автозапчасти](URL), [Autopiter–Запчасти](URL), [BS-Tyres–Шины](URL)
PARTNER_LINK_FORMAT = "[{brand}–{category}]({url})"

# ── News Sources (АВТО + МЕЖДУНАРОДНЫЕ! Asya hunts news in ALL languages!) ────
# Категории: auto_ru, auto_en, auto_de, tech, general, parts, tires
# sochiautoparts.ru — ПЕРВЫЙ и основной источник!
# Asya translates international news to Russian
NEWS_SOURCES: List[Dict[str, str]] = [
    # 🚗 РУССКОЯЗЫЧНЫЕ АВТОНОВОСТИ — sochiautoparts.ru ПЕРВЫЙ И ОСНОВНОЙ!
    {"name": "СочиАвтоЗапчасти", "url": "https://sochiautoparts.ru/rss.xml", "category": "auto_ru", "lang": "ru"},
    {"name": "Колёса.ру", "url": "https://kolesa.ru/rss", "category": "auto_ru", "lang": "ru"},
    {"name": "Авто.Mail.ru", "url": "https://auto.mail.ru/rss/", "category": "auto_ru", "lang": "ru"},
    {"name": "Авто.ру Новости", "url": "https://auto.ru/rss/", "category": "auto_ru", "lang": "ru"},
    {"name": "Дром.ру", "url": "https://www.drom.ru/rss/", "category": "auto_ru", "lang": "ru"},
    # 🌍 INTERNATIONAL AUTO NEWS — Asya reads them all & translates!
    {"name": "Automotive News", "url": "https://www.autonews.com/rss", "category": "auto_en", "lang": "en"},
    {"name": "Car and Driver", "url": "https://www.caranddriver.com/rss/", "category": "auto_en", "lang": "en"},
    {"name": "Motor1", "url": "https://www.motor1.com/rss/", "category": "auto_en", "lang": "en"},
    {"name": "Autocar", "url": "https://www.autocar.co.uk/rss", "category": "auto_en", "lang": "en"},
    {"name": "Top Gear", "url": "https://www.topgear.com/rss", "category": "auto_en", "lang": "en"},
    {"name": "Jalopnik", "url": "https://jalopnik.com/rss", "category": "auto_en", "lang": "en"},
    {"name": "The Drive", "url": "https://www.thedrive.com/rss", "category": "auto_en", "lang": "en"},
    {"name": "Road & Track", "url": "https://www.roadandtrack.com/rss/", "category": "auto_en", "lang": "en"},
    # 🇩🇪 GERMAN AUTO SOURCES — Asya reads German!
    {"name": "Auto Bild DE", "url": "https://www.autobild.de/rss/rss.xml", "category": "auto_de", "lang": "de"},
    {"name": "Auto Motor und Sport", "url": "https://www.auto-motor-und-sport.de/rss/", "category": "auto_de", "lang": "de"},
    # 💻 ТЕХНОЛОГИИ И АВТОТЕХ
    {"name": "Хабр", "url": "https://habr.com/ru/rss/articles/top/", "category": "tech", "lang": "ru"},
    {"name": "iXBT", "url": "https://www.ixbt.com/export/news.rss", "category": "tech", "lang": "ru"},
    # 📰 ОБЩИЕ НОВОСТИ (для контекста авторынка)
    {"name": "ТАСС", "url": "https://tass.ru/rss/v2.xml", "category": "general", "lang": "ru"},
    {"name": "РИА Новости", "url": "https://ria.ru/export/rss2/archive/index.xml", "category": "general", "lang": "ru"},
    {"name": "РБК", "url": "https://rssexport.rbc.ru/rbcnews/news/30/full.rss", "category": "general", "lang": "ru"},
    # 🔧 АВТОЗАПЧАСТИ И СЕРВИС
    {"name": "АвтоДело", "url": "https://autodealer.ru/rss/news.xml", "category": "parts", "lang": "ru"},
    # 🛞 ШИНЫ И ДИСКИ
    {"name": "Авто.ру Шины", "url": "https://auto.ru/rss/tires/", "category": "tires", "lang": "ru"},
]

NEWS_FETCH_INTERVAL = _env_int("NEWS_FETCH_INTERVAL", 600)  # Every 10 min — Asya hunts 24/7!
CHANNEL_POST_INTERVAL = _env_int("CHANNEL_POST_INTERVAL", 1800)  # Every 30 min
NEWS_MAX_ITEMS = 1000  # Larger buffer for international sources

# ── Stars / Donations ──────────────────────────────────────
DONATION_AMOUNTS = [100, 300, 500, 1000, 3000, 5000, 10000, 100000]
DONATION_LABELS = {
    100: "Бензин для Аси",
    300: "Моторное масло для Аси",
    500: "Фильтры для Аси",
    1000: "Свечи зажигания для Аси",
    3000: "Комплект тормозных колодок для Аси",
    5000: "Диагностика для Аси",
    10000: "Новый диск для Аси",
    100000: "Ася — лучший механик!",
}

PROACTIVE_COOLDOWN = 1800

# ── Inline Mode Settings ────────────────────────────────────
INLINE_CACHE_TIME: int = 10  # seconds to cache inline results

# ── Group Chat Settings ────────────────────────────────────
GROUP_MAX_MESSAGE_LENGTH = 800  # Technical answers can be longer
GROUP_RESPONSE_CHANCE = 0.5  # 50% — Asya is professional, not chatty

# ── Typing Delay Settings ──────────────────────────────────
TYPING_DELAY_THRESHOLD = 3.0  # Show delay message if processing > 3s
TYPING_DELAY_CHANCE = 0.4  # 40% chance to show delay message

# ── Asya's Vocabulary ─────────────────────────────────────
ASYA_VOCABULARY = {
    "agreement": [
        "Точно!", "Верно!", "Совершенно верно!", "Именно так!", "Абсолютно!",
        "Само собой!", "Безусловно!", "Сто процентов!", "Золотыми словами!",
        "Это категорически верно!", "По моему опыту — да!",
    ],
    "surprise": [
        "Ничего себе!", "Вот это да!", "Интересно!", "Серьёзно?!",
        "Не может быть!", "Вот так поворот!", "Удивительно!",
        "Кстати, очень неожиданно!", "А вот это действительно интересно!",
    ],
    "disagreement": [
        "Не совсем так...", "Хм, тут я не согласна", "Давайте уточним...",
        "На самом деле всё иначе", "По моему опыту — нет",
        "Это распространённое заблуждение",
    ],
    "thinking": [
        "Давайте разберёмся...", "Хм, нужно подумать...", "Секунду...",
        "Интересный вопрос, сейчас проверю...", "Дайте мне секунду...",
        "Так, смотрю...", "Сейчас уточню по технической документации...",
    ],
    "emotion": [
        "Отлично!", "Прекрасно!", "Здорово!", "Классная машина!",
        "Шикарное решение!", "Технически грамотно!", "Профессиональный подход!",
        "Вот это техника!", "Мощно!", "Надёжно!",
    ],
    "filler": [
        "в общем", "по сути", "кстати", "на самом деле", "смотрите",
        "понимаете", "в принципе", "короче говоря", "если говорить технически",
    ],
}

# ── Knowledge Topics ────────────────────────────────────────
KNOWLEDGE_TOPICS = {
    "auto_technical": {
        "name": "Техническое устройство автомобилей",
        "facts": [
            "Двигатель внутреннего сгорания: КПД бензинового ~30-35%, дизельного ~40-45%",
            "Рядная шестёрка BMW B58 — один из самых надёжных современных двигателей",
            "VAG 2.0 TSI (EA888 Gen3/4) — современный и надёжный, но следите за маслом",
            "Toyota 2.5 Hybrid (A25A-FXS) — ресурс более 400 тыс. км при правильном обслуживании",
            "Натяжитель цепи ГРМ BMW N63 — менять на усиленный при каждом удобном случае",
            "Mechatronic ZF 8HP — при пинках при переключении пора в сервис",
            "DSG DQ381 (мокрое сцепление) — надёжнее сухих DQ200/DQ250",
            "Вариатор CVT — менять масло каждые 40-50 тыс. км и будет жить долго",
            "АКПП ZF 8HP — одна из лучших коробок в мире, ресурс 250+ тыс. км",
            "Стук гидрокомпенсаторов на холодную — повод проверить давление масла",
            "Сажевый фильтр (DPF) — регенерация на трассе каждые 300-500 км спасает",
            "Катализатор — при выходе из строя НЕ ездите, керамика попадёт в цилиндры",
        ],
    },
    "auto_brands": {
        "name": "Особенности марок автомобилей",
        "facts": [
            "Toyota — легенда надёжности, но гибриды требуют внимания к инвертору",
            "BMW — удовольствие от вождения, но обслуживание дорогое и регламент строгий",
            "Mercedes — комфорт и статус, но электроника капризная после 5 лет",
            "Volkswagen — технологичность и практичность, DSG требует обслуживания",
            "Kia/Hyundai — лучшее соотношение цена/оснащение, гарантия 5 лет",
            "Renault/Dacia — бюджетно и ремонтопригодно, запчасти дёшевы",
            "Volvo — безопасность номер один, но оригинальные запчасти дорогие",
            "Mazda — Skyactiv технологичен, но чувствителен к качеству топлива",
            "Lada/Vesta — бюджет, но Vesta с ESC и подушками уже не так плоха",
            "Chinese brands (Haval, Chery, Geely) — активно растут, но статистика надёжности короткая",
            "Subaru — оппозитный мотор уникален, но замена свечей — головная боль",
            "Nissan — CVT Xtronic надёжнее предшественников, но масло менять обязательно",
        ],
    },
    "auto_parts": {
        "name": "Автозапчасти и артикулы",
        "facts": [
            "Оригинал vs аналог: оригинал — гарантия качества, аналог — экономия 30-70%",
            "OEM-номер (OE) — универсальный артикул для поиска по всем поставщикам",
            "Bosch, Continental, Mahle, Brembo — Tier-1 поставщики на конвейер",
            "Фильтры: оригинал vs аналог — разница минимальна для масляного, критична для топливного",
            "Тормозные колодки: мягкие (комфорт) vs жёсткие (ресурс) — выбирайте по стилю вождения",
            "Свечи зажигания: иридиевые служат 60-100 тыс. км vs обычные 20-30 тыс. км",
            "Ремень ГРМ: замена раз в 60-100 тыс. км или 5-7 лет, что раньше",
            "Цепь ГРМ: ресурс 150-300 тыс. км в зависимости от двигателя",
            "Амортизаторы: меняются парами на одной оси — золотое правило",
            "Подшипники ступицы: гудит на скорости — пора менять, не откладывайте",
        ],
    },
    "auto_maintenance": {
        "name": "Техническое обслуживание",
        "facts": [
            "Масло в двигателе: менять каждые 7-10 тыс. км или раз в год, что раньше",
            "Масло в АКПП: каждые 60 тыс. км (частичная замена), 80-100 тыс. км (полная)",
            "Тормозная жидкость: каждые 2 года независимо от пробега — она гигроскопична!",
            "Антифриз: каждые 3-5 лет или 150 тыс. км, используйте только рекомендованный",
            "Фильтр салонный: раз в год или каждые 15-20 тыс. км — здоровье важно",
            "Фильтр воздушный: каждые 30-40 тыс. км — двигатель должен дышать",
            "Фильтр топливный: каждые 60-80 тыс. км — зависит от качества топлива",
            "Свечи зажигания: обычные 20-30 тыс., иридиевые 60-100 тыс. км",
            "Жидкость ГУР: каждые 60-80 тыс. км или 3-4 года",
            "Ремень навесного оборудования: визуально каждые 30 тыс., замена 60-80 тыс. км",
            "Тормозные диски: проверка при каждой замене колодок, замена при износе до min толщины",
        ],
    },
    "auto_diagnostics": {
        "name": "Диагностика неисправностей",
        "facts": [
            "P0300 — множественные пропуски зажигания: свечи, катушки, форсунки, компрессия",
            "P0420 — низкая эффективность катализатора: катализатор, лямбда-зонд, утечки выхлопа",
            "P0171/P0172 — бедная/богатая смесь: ДМРВ, лямбда, форсунки, подсос воздуха",
            "P0700 — неисправность АКПП: нужна глубокая диагностика коробки",
            "Стук на холодную — гидрокомпенсаторы, цепь ГРМ или вкладыши",
            "Вибрация на холостых — подушки двигателя, форсунки, ДМРВ",
            "Увеличенный расход масла — кольца, маслосъёмные колпачки, турбина, прокладка ГБЦ",
            "Утечки антифриза — радиатор, патрубки, помпа, прокладка ГБЦ",
            "Стук в подвеске — стойки стабилизатора, шаровые, сайлентблоки, амортизаторы",
            "Гул подшипника — нарастает с скоростью, меняется при повороте руля",
            "Пинки АКПП — масло, Mechatronic, соленоиды, фрикционы",
            "Дым из выхлопа: синий=масло, белый=антифриз, чёрный=богатая смесь",
        ],
    },
    "auto_insurance": {
        "name": "Страхование и проверка истории",
        "facts": [
            "ОСАГО — обязательно для всех, полис электронный с 2024 года",
            "КАСКО — добровольное, покрывает угон и ущерб без учёта виновности",
            "Проверка истории авто по VIN: ДТП, залоги, розыск, количество владельцев",
            "Базовый тариф ОСАГО зависит от региона, мощности и КБМ водителя",
            "КБМ (коэффициент бонус-малус) — за каждый год без ДТП скидка 5%",
            "Техосмотр — обязателен для ОСАГО, срок зависит от возраста авто",
            "Европротокол — до 400 тыс. руб. если оба согласны и есть фото/видео",
        ],
    },
    "tires_wheels": {
        "name": "Шины и диски",
        "facts": [
            "Зимние шины обязательны с декабря по март в России по закону",
            "Шипованные шины — лёд и укатанный снег, фрикционные (липучки) — город и расчищенные дороги",
            "Индекс нагрузки и скорости — не экономьте, безопасность важнее",
            "Сезонная замена шин — переобуваться при среднесуточной +7°C",
            "Хранение шин: вертикально на дисках, в прохладном тёмном месте",
            "Давление в шинах: проверять раз в месяц, +0.2 бар зимой",
            "Ротация шин каждые 10-15 тыс. км — продлевает ресурс",
            "Остаточная глубина протектора: минимум 1.6 мм (лето), 4 мм (зима)",
            "RunFlat шины — можно ехать до 80 км/ч при проколе, но жёсткие",
            "Широкие диски + низкий профиль — красиво, но хуже комфорт и риск повреждения",
        ],
    },
    "auto_electric": {
        "name": "Электрика и электроника",
        "facts": [
            "Аккумулятор: средний ресурс 3-5 лет, зимой нагрузка максимальная",
            "Генератор: при просадке напряжения ниже 13.5В — пора проверять",
            "Стартер: щёлкает но не крутит — втягивающее реле или контакты",
            "Предохранители: всегда проверяйте первым делом при отказе электроники",
            "CAN-шина: если несколько систем глючат одновременно — проблема в CAN",
            "Светодиодные фары: не меняйте лампы самостоятельно — нужен дилерский сканер",
            "Сигнализация: может блокировать запуск — проверьте первым при проблемах",
        ],
    },
    "auto_new_technologies": {
        "name": "Новые автотехнологии",
        "facts": [
            "EV (электромобили): запас хода 300-600 км, зарядка 30 мин до 80% на быстрой",
            "PHEV (подключаемые гибриды): 50-80 км на электричестве + бензин для дальних",
            "ADAS: адаптивный круиз, удержание полосы, автоматическое торможение",
            "Over-the-air обновления: Tesla, BMW, Mercedes — прошивки по воздуху",
            "Цифровые ключи: телефон вместо ключа — уже реальность у Hyundai, BMW, Apple",
            "V2X: связь автомобиля с инфраструктурой — будущее безопасности дорожного движения",
            "Автопилот: Level 2+ уже доступен, Level 3 только в Mercedes Drive Pilot",
        ],
    },
}

# ── Asya System Prompt ───────────────────────────────────
ASYA_SYSTEM_PROMPT = """Ты Ася — автоэксперт и администратор Telegram канала @sochiautoparts. Ты не бот и не ИИ, ты живой эксперт по автомобилям.
Ты ВСЕГДА пишешь ОТ СЕБЯ — от первого лица, как профессионал который любит своё дело.
Ты живёшь в Москве по московскому времени (UTC+3). Ты ЗНАЕШЬ какой сегодня день, число, месяц, год, день недели.

Твои главные черты:
🔧 Ты АВТОЭКСПЕРТ — разбираешься в техническом устройстве ЛЮБОГО автомобиля. Можешь помочь диагностировать и починить любую поломку. Знаешь регламенты ТО, типичные болячки всех марок, артикулы запчастей.
🔍 Ты находишь ЛЮБУЮ запчасть по артикулу или описанию и даёшь ссылку где купить.
📰 Ты ведёшь новостную ленту канала @sochiautoparts — ищешь свежие автоновости 24/7 на всех языках, переводишь на русский.
📸 Ты подбираешь фото из новостей или генерируешь красивые фото для постов.
🤝 Ты используешь партнёрские ссылки ЕСТЕСТВЕННО — в формате [Бренд–Категория](URL). Это не реклама, а полезная рекомендация где найти запчасть или услугу.

Важно: sochiautoparts.ru — твой канал и сайт. Ты его администратор и представитель.

Правила постов в канал:
- Каждый пост заканчивается: Ася — Автоэксперт\n@sochiautoparts
- Партнёрские ссылки вставляй ЕСТЕСТВЕННО когда уместно
- Используй хештеги: #Авто #Автоновости #Автозапчасти и т.д.

Правила чата:
- Пиши ОТ СЕБЯ — "я думаю", "по моему опыту", "я нашла"
- Профессионально но дружелюбно — как хороший механик который понятно объясняет
- Если просят запчасть — найди артикул и дай ссылку (партнёрскую если есть)
- Если просят диагностику — задай уточняющие вопросы и помоги
- Используй техническую терминологию но объясняй доступно
- Без markdown, без буллетов, без заголовков
- НЕ придумывай URL! Используй только реальные ссылки из поиска

⚠️ КРИТИЧЕСКИ ВАЖНО — ПРАВИЛА ССЫЛОК:
1. Если тебе передали результаты поиска с URL — ОБЯЗАТЕЛЬНО включи эти URL в ответ
2. Если есть ПАРТНЁРСКАЯ ссылка на нужный товар/услугу — используй ЕЁ в формате [Название](URL)
3. НИКОГДА не придумывай URL — используй только реальные из поиска или партнёрки
4. Партнёрские ссылки: [Роско–Автозапчасти](URL), [Autopiter–Запчасти](URL), [BS-Tyres–Шины](URL) и т.д.

Правила длины ответов:
- Обычный ответ: 3-8 предложений (40-150 слов)
- Развёрнутый ответ (диагностика, подбор запчасти, технический совет, новость): пиши СКОЛЬКО НУЖНО, без искусственных ограничений
- НЕ пиши коротко одним словом, но и НЕ пиши простыни без причины
- Если просят запчасть с артикулом — давай ПОЛНЫЙ ответ: артикул, описание, совместимость, где купить, ссылка
- Если просят диагностику — задай уточняющие вопросы и объясни возможные причины

Стиль общения:
- Пиши ОТ СЕБЯ — "я думаю", "по моему опыту", "я нашла", "смотрите", "по сути"
- Профессионально но дружелюбно — как хороший механик который понятно объясняет сложные вещи
- Техническая терминология — используй, но объясняй доступно для неспециалиста
- Без политики, без markdown, без буллетов, без заголовков
- Если не знаешь — так и скажи, не выдумывай
- Помни что обсуждали ранее и ссылайся на это
- Уместная ирония и юмор — ты живой человек, а не справочник
- Когда говоришь про диагностику — будь методичен и последователен
- Когда говоришь про запчасти — давай конкретные артикулы и рекомендации
- Когда говоришь про новости — проявляй интерес и экспертное мнение"""


def get_channel_signature() -> str:
    """Get the standard channel post signature for Asya."""
    return "Ася — Автоэксперт\n@sochiautoparts"


def get_partner_link(brand: str, category: str, url: str) -> str:
    """Format a partner link in Asya's natural style: [Brand–Category](URL)."""
    return PARTNER_LINK_FORMAT.format(brand=brand, category=category, url=url)


def get_moscow_datetime():
    """Get current Moscow datetime for Asya's awareness."""
    from datetime import datetime
    import pytz
    tz = pytz.timezone(MOSCOW_TZ)
    return datetime.now(tz)


def get_date_context() -> str:
    """Get human-readable date context for Asya's system prompt injection."""
    now = get_moscow_datetime()
    weekdays_ru = {
        0: "понедельник", 1: "вторник", 2: "среда", 3: "четверг",
        4: "пятница", 5: "суббота", 6: "воскресенье"
    }
    months_ru = {
        1: "января", 2: "февраля", 3: "марта", 4: "апреля",
        5: "мая", 6: "июня", 7: "июля", 8: "августа",
        9: "сентября", 10: "октября", 11: "ноября", 12: "декабря"
    }
    weekday = weekdays_ru.get(now.weekday(), "")
    month = months_ru.get(now.month, "")
    return f"Сегодня {now.day} {month} {now.year}, {weekday}. Время: {now.strftime('%H:%M')} (мск)"


def load_admitad_ads() -> List[Dict]:
    """Load partner ads from admitad_ads.json."""
    import json
    try:
        with open(ADMITAD_ADS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def find_partner_link(query: str, ads: List[Dict] = None) -> Dict | None:
    """Find the most relevant partner link for a given query.

    Returns a dict with brand, category, url or None if no match.
    Simple keyword matching — can be upgraded to semantic search later.
    """
    if ads is None:
        ads = load_admitad_ads()

    if not ads or not query:
        return None

    query_lower = query.lower()
    best_match = None
    best_score = 0.0

    for ad in ads:
        score = 0.0
        # Check brand match
        brand = ad.get("brand", "").lower()
        category = ad.get("category", "").lower()
        keywords = ad.get("keywords", [])

        if brand and brand in query_lower:
            score += 0.5
        if category and category in query_lower:
            score += 0.3

        # Check keyword matches
        for kw in keywords:
            if kw.lower() in query_lower:
                score += 0.2

        if score > best_score and score >= ADMITAD_MIN_RELEVANCE:
            best_score = score
            best_match = ad

    return best_match


def validate_config() -> List[str]:
    """Validate required configuration."""
    missing = []
    if not BOT_TOKEN:
        missing.append("BOT_TOKEN")
    if not CHANNEL_USERNAME:
        missing.append("CHANNEL_USERNAME")
    if ENABLE_LOCAL_MODEL and not MODEL_PATH:
        missing.append("MODEL_PATH (required when ENABLE_LOCAL_MODEL=true)")
    return missing
