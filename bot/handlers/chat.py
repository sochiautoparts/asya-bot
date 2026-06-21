"""
Chat Handler — Main user interaction with AI, web search, partner links,
car diagnostics, spare part search, VIN decoding, photo analysis,
and personalized communication with conversation context.
"""

import re
import random
import logging
import base64
from typing import Optional

from aiogram import Router, F, types
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery, PhotoSize, WebAppInfo
from aiogram.enums import ChatAction
from aiogram.client.default import DefaultBotProperties
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.config import config, persona
from bot.database import (
    get_or_create_user, is_user_blocked, add_chat_message,
    clear_chat_history, get_chat_mode, set_chat_mode,
    add_user_car, get_user_cars, delete_user_car, update_car_mileage,
    check_rate_limit,
)
from bot.asya import (
    is_part_number, extract_part_numbers, identify_car_brand,
    detect_symptoms, detect_obd2_codes, lookup_obd2_code,
    build_diagnostic_context, ASYA_PHRASES,
)
from bot.web_search import web_search, search_spare_part, search_parts_by_vin, format_search_results
from bot.tech_docs import (
    search_part_by_article, search_diagnostic_code,
    search_repair_procedure, format_part_info, format_tech_context,
)
from bot.partners import partner_manager
from ai.router import ai_router
from ai.voice import process_voice_message

# v5.1 optimizations
from bot.optimizations import (
    find_full_urls,
    find_bare_domains,
    get_request_deduplicator,
    adaptive_max_chars,
    chat_type_context,
)

logger = logging.getLogger("asya.handlers.chat")

chat_router = Router()

# ── Character limits for chat responses ──────────────────────────────────────
CHAT_MAX_CHARS = 1500    # Private chat max (system prompt asks for 500-1000, this is hard limit)
GROUP_MAX_CHARS = 600    # Group/supergroup max (system prompt asks for 300, this is hard limit)
COMMENT_MAX_CHARS = 300  # Comments in other groups (not Ася's own channel)


# ── VIN / Body number detection ───────────────────────────────────────────────

_VIN_PATTERN = re.compile(r'\b[A-HJ-NPR-Z0-9]{17}\b', re.IGNORECASE)
# Also match VINs with spaces or dashes (users often type them separated)
_VIN_FLEX_PATTERN = re.compile(
    r'(?:VIN[-:]?\s*|вин[-:]?\s*|вин-код[-:]?\s*)?([A-HJ-NPR-Z0-9](?:[A-HJ-NPR-Z0-9\s\-]{14,22})[A-HJ-NPR-Z0-9])',
    re.IGNORECASE
)
_BODY_NUMBER_PATTERN = re.compile(
    r'(?:номер\s+кузова|кузовн?ой\s+номер|body\s*number|кузов)\s*[:\s]*([A-Z0-9\-/]{5,20})',
    re.IGNORECASE
)


def _detect_vin(text: str) -> Optional[str]:
    """Detect a VIN code (17 chars) in text."""
    match = _VIN_PATTERN.search(text.upper())
    if match:
        vin = match.group(0)
        # Validate check digit position (9th char)
        if len(vin) == 17 and vin[8] in '0123456789X':
            return vin
    return None


def _detect_body_number(text: str) -> Optional[str]:
    """Detect a body number reference in text."""
    match = _BODY_NUMBER_PATTERN.search(text)
    if match:
        return match.group(1)
    return None


def _is_vin_query(text: str) -> bool:
    """Check if text is asking about VIN/body number decoding."""
    text_lower = text.lower()
    keywords = [
        "vin", "вин", "номер кузова", "кузовной номер", "расшифруй vin",
        "расшифруй вин", "пробей vin", "пробей вин", "декодировать vin",
        "vin код", "вин код", "vin-код", "вин-код",
        "что за vin", "что за вин", "какая машина vin", "какая машина вин",
        "какой автомобиль vin", "определи vin", "определи вин",
        "что за машина vin", "проверь vin", "проверь вин",
        "история vin", "история автомобиля", "пробить машину",
    ]
    return any(kw in text_lower for kw in keywords)


# ── Gender detection from Russian first name ────────────────────────────────

MALE_NAME_ENDINGS = ("й", "ь", "н", "л", "р", "с", "т", "в", "к", "м", "г", "б", "д", "п", "з", "ж", "х")
FEMALE_NAME_ENDINGS = ("а", "я", "ия", "ья", "ина")

COMMON_MALE_NAMES = {
    "александр", "дмитрий", "максим", "сергей", "андрей", "алексей", "артём",
    "илья", "кирилл", "михаил", "никита", "матвей", "роман", "егор", "арсений",
    "иван", "денис", "евгений", "даниил", "тимур", "владимир", "олег", "павел",
    "руслан", "вадим", "константин", "антон", "борис",
}

COMMON_FEMALE_NAMES = {
    "анна", "мария", "ольга", "елена", "наталья", "татьяна", "ирина", "светлана",
    "екатерина", "юлия", "дарья", "алина", "вера", "полина", "кристина", "софия",
    "валерия", "марина", "людмила", "надежда", "настя", "анастасия",
    "виктория", "маргарита", "диана", "евгения", "алёна", "катерина",
}


def _guess_gender(first_name: str) -> str:
    """Guess gender from Russian first name. Returns 'male', 'female', or 'unknown'."""
    if not first_name:
        return "unknown"

    name_lower = first_name.lower().strip()

    if name_lower in COMMON_MALE_NAMES:
        return "male"
    if name_lower in COMMON_FEMALE_NAMES:
        return "female"

    if name_lower.endswith(FEMALE_NAME_ENDINGS):
        if name_lower.endswith("ь"):
            pass
        else:
            return "female"

    if name_lower.endswith("й") or name_lower.endswith("ь"):
        return "male"

    return "unknown"


def _get_user_persona_context(message: Message) -> str:
    """Build a context string about the user for personalized communication."""
    parts = []

    first_name = message.from_user.first_name or ""
    last_name = message.from_user.last_name or ""
    username = message.from_user.username or ""

    if first_name:
        parts.append(f"Имя пользователя: {first_name}")
    if last_name:
        parts.append(f"Фамилия: {last_name}")
    if username:
        parts.append(f"Username: @{username}")

    # Guess gender
    gender = _guess_gender(first_name)
    if gender == "male":
        parts.append("Пол: скорее всего мужчина")
    elif gender == "female":
        parts.append("Пол: скорее всего женщина")

    # Is this the owner?
    if message.from_user.id == config.OWNER_ID:
        parts.append("Это владелец бота — общайся тепло и уважительно")

    if parts:
        return "Информация о пользователе для персонализации общения:\n" + "\n".join(parts)
    return ""


# ── Middleware-like: check user and log ─────────────────────────────────────────

async def _check_user(message: Message) -> bool:
    """Check if user is allowed to interact. Returns True if allowed."""
    user = await get_or_create_user(
        user_id=message.from_user.id,
        username=message.from_user.username or "",
        first_name=message.from_user.first_name or "",
        last_name=message.from_user.last_name or "",
        language_code=message.from_user.language_code or "ru",
    )

    if await is_user_blocked(message.from_user.id):
        return False

    # Rate limiting
    if not check_rate_limit(message.from_user.id):
        await message.answer("Ты слишком быстро пишешь! Дай мне секунду переварить ")
        return False

    return True


# ── /start command ─────────────────────────────────────────────────────────────

@chat_router.message(CommandStart())
async def cmd_start(message: Message):
    """Handle /start command — greet like a living person, not a service bot."""
    if not await _check_user(message):
        return

    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    name = message.from_user.first_name or ""
    gender = _guess_gender(name)

    from datetime import datetime
    from zoneinfo import ZoneInfo
    import os
    hour = datetime.now(ZoneInfo("Europe/Moscow")).hour

    # Add Mini App button to /start
    miniapp_url = os.getenv("MINIAPP_URL", "https://asiaexp-bot-miniapp.onrender.com")
    builder = InlineKeyboardBuilder()
    builder.button(text="🚗 Открыть Асю", web_app=WebAppInfo(url=miniapp_url))

    if name:
        if gender == "male":
            greets = [
                f"Привет, {name}! 😊 Ася тут. Чем займёмся?",
                f"Хей, {name}! О чём поболтаем?",
                f"О, {name}! Привет! Просто пиши, я всегда рада поболтать",
                f"Привет, {name}! 😊 Кофе уже пью, можно общаться",
            ]
        elif gender == "female":
            greets = [
                f"Привет, {name}! 😊 Мы с тобой обе понимаем толк в хорошем общении!",
                f"Хей, {name}! Давай поболтаем!",
                f"Привет, {name}! 😊 Всегда рада компании!",
            ]
        else:
            greets = [
                f"Привет, {name}! 😊 Рад(а) знакомству!",
                f"Хей, {name}! О чём поговорим?",
            ]
    else:
        greets = [
            "Привет! 😊 Просто пиши — я всегда на связи!",
            "Хей! Давай знакомиться!",
            "Привет! 😊 Пиши о чём хочешь, я люблю общаться!",
        ]

    welcome = random.choice(greets)
    await message.answer(welcome, reply_markup=builder.as_markup())


# ── /help command ──────────────────────────────────────────────────────────────

@chat_router.message(Command("help"))
async def cmd_help(message: Message):
    """Handle /help command — casual, living-person style."""
    if not await _check_user(message):
        return

    help_text = (
        "Если что, я могу:\n\n"
        "🔧 Помочь с диагностикой — расскажи, что с машиной, разберёмся вместе\n"
        "🔍 Подобрать запчасти — подскажу где искать по VIN и артикулу\n"
        "📊 Расшифровать VIN или номер кузова — просто отправь\n"
        "📸 Посмотреть фото — отправь, я расскажу что вижу\n"
        "💬 Просто поболтать — я люблю общаться на любые темы!\n"
        "🚗 Сохранить твою машину — /mycar Марка Модель Год\n"
        "📱 Работаю в любом чате — набери @asiaexp_bot и вопрос!\n\n"
        "Команды:\n"
        "/app — открыть мини-приложение\n"
        "/clear — начать с чистого листа\n"
        "/diagnostic — фокус на диагностике\n"
        "/parts — ищем запчасти\n"
        "/normal — обычный режим\n"
        "/mycar — мои машины\n"
        "/delcar <номер> — удалить машину\n"
        "/mileage <номер> <км> — обновить пробег"
    )
    await message.answer(help_text)


# ── /app command — Open Mini App ────────────────────────────────────────────────

@chat_router.message(Command("app"))
async def cmd_app(message: Message):
    """Handle /app command — open the Mini App."""
    if not await _check_user(message):
        return

    import os
    miniapp_url = os.getenv("MINIAPP_URL", "https://asiaexp-bot-miniapp.onrender.com")

    builder = InlineKeyboardBuilder()
    builder.button(text="🚗 Открыть Асю", web_app=WebAppInfo(url=miniapp_url))

    await message.answer(
        "Открой мини-приложение Аси — там всё удобнее!\n\n"
        "🔧 Диагностика\n🔑 VIN-расшифровка\n🔍 Поиск запчастей\n💬 Чат с Асей",
        reply_markup=builder.as_markup(),
    )


# ── /clear command ─────────────────────────────────────────────────────────────

@chat_router.message(Command("clear"))
async def cmd_clear(message: Message):
    """Clear chat history."""
    if not await _check_user(message):
        return

    await clear_chat_history(message.from_user.id)
    await message.answer("Чистый лист! 😊 Начинаем заново")


# ── Mode commands ──────────────────────────────────────────────────────────────

@chat_router.message(Command("diagnostic"))
async def cmd_diagnostic(message: Message):
    """Switch to diagnostic mode."""
    if not await _check_user(message):
        return

    await set_chat_mode(message.from_user.id, "diagnostic")
    await message.answer(
        "Ок, режим диагностики 🔧 Расскажи, что с машиной — разберёмся вместе"
    )


@chat_router.message(Command("parts"))
async def cmd_parts(message: Message):
    """Switch to parts search mode."""
    if not await _check_user(message):
        return

    await set_chat_mode(message.from_user.id, "parts")
    await message.answer(
        "Ищем запчасти 🔍 Подскажу где искать — Росско, Autopiter, AvtoALL"
    )


@chat_router.message(Command("normal"))
async def cmd_normal(message: Message):
    """Switch to normal chat mode."""
    if not await _check_user(message):
        return

    await set_chat_mode(message.from_user.id, "normal")
    await message.answer("Обычный режим 😊 Пиши о чём хочешь!")


# ── /mycar command — User car profiles ────────────────────────────────────────

@chat_router.message(Command("mycar"))
async def cmd_mycar(message: Message):
    """Show user's saved cars or add a new one."""
    if not await _check_user(message):
        return

    args = message.text.split(maxsplit=1)

    if len(args) < 2:
        # Show saved cars
        cars = await get_user_cars(message.from_user.id)
        if not cars:
            await message.answer(
                "У тебя пока нет сохранённых машин. Добавь:\n"
                "/mycar Toyota Camry 2020\n"
                "/mycar BMW X5 2019 B58 65000\n"
                "\nФормат: /mycar Марка Модель Год [Двигатель] [Пробег]"
            )
            return

        lines = ["🚗 Твои машины:"]
        for car in cars:
            car_info = f"  {car['brand']} {car['model']}"
            if car['year']:
                car_info += f" {car['year']}"
            if car['engine']:
                car_info += f", {car['engine']}"
            if car['mileage']:
                car_info += f", {car['mileage']} км"
            car_info += f" (#{car['id']})"
            lines.append(car_info)
            if car['vin']:
                lines.append(f"    VIN: {car['vin']}")

        lines.append("\nУдалить: /delcar <номер>")
        lines.append("Обновить пробег: /mileage <номер> <км>")
        await message.answer("\n".join(lines))
        return

    # Parse and add a car
    car_text = args[1].strip()
    parts = car_text.split()

    brand = parts[0] if len(parts) > 0 else ""
    model_name = parts[1] if len(parts) > 1 else ""
    year = 0
    engine = ""
    mileage = 0

    try:
        year = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    except (ValueError, IndexError):
        pass

    # Engine and mileage from remaining parts
    remaining = parts[3:] if year else parts[2:]
    for r in remaining:
        if r.isdigit() and len(r) >= 4:
            mileage = int(r)
        elif not engine:
            engine = r
        else:
            engine += f" {r}"

    car_id = await add_user_car(
        user_id=message.from_user.id,
        brand=brand,
        model=model_name,
        year=year,
        engine=engine,
        mileage=mileage,
    )

    await message.answer(f"Машина добавлена! {brand} {model_name} {year or ''} (#{car_id}) 🚗")


# ── /delcar command ────────────────────────────────────────────────────────────

@chat_router.message(Command("delcar"))
async def cmd_delcar(message: Message):
    """Delete a car from user's profile."""
    if not await _check_user(message):
        return

    args = message.text.split()
    if len(args) < 2:
        await message.answer("Использование: /delcar <номер машины>")
        return

    try:
        car_id = int(args[1])
    except ValueError:
        await message.answer("Нужно указать номер машины (число)")
        return

    deleted = await delete_user_car(car_id, message.from_user.id)
    if deleted:
        await message.answer("Машина удалена из профиля ✅")
    else:
        await message.answer("Не найдена такая машина в твоём профиле")


# ── /mileage command ──────────────────────────────────────────────────────────

@chat_router.message(Command("mileage"))
async def cmd_mileage(message: Message):
    """Update mileage for a saved car."""
    if not await _check_user(message):
        return

    args = message.text.split()
    if len(args) < 3:
        await message.answer("Использование: /mileage <номер машины> <пробег км>")
        return

    try:
        car_id = int(args[1])
        km = int(args[2])
    except ValueError:
        await message.answer("Нужно: номер машины и пробег (числа)")
        return

    updated = await update_car_mileage(car_id, message.from_user.id, km)
    if updated:
        await message.answer(f"Пробег обновлён: {km} км 📝")
    else:
        await message.answer("Не найдена такая машина")


# ── Photo handler ──────────────────────────────────────────────────────────────

@chat_router.message(F.photo)
async def handle_photo(message: Message):
    """Handle photo messages — analyze with vision AI.

    In GROUPS/SUPERGROUPS: use LOCAL-ONLY routing (comments mode).
    Only use cloud vision for private chats (1-on-1 with user).
    """
    if not await _check_user(message):
        return

    # Check if we're in a group/supergroup — use LOCAL-ONLY for comments
    is_group = message.chat.type in ("group", "supergroup")

    # In groups: Ася should just comment on local model ONLY, not use cloud vision
    if is_group:
        # LOCAL MODEL ONLY for group comments — no cloud API waste!
        # Use singleton from ai_router — no reloading the model every time!
        from ai.router import ai_router
        local_provider = ai_router._local
        if local_provider and await local_provider.is_available():
            caption = message.caption or ""
            simple_prompt = (
                f"Кто-то прислал фото в группе. "
                f"{'С подписью: ' + caption[:100] if caption else 'Без подписи.'} "
                f"Напиши короткий комментарий (до 200 символов) как автоэксперт. "
                f"Без анализа фото — просто живой комментарий."
            )
            try:
                messages = [
                    {"role": "system", "content": "Ты Ася — автоэксперт. Короткие комментарии до 200 символов. Без markdown. Без политики."},
                    {"role": "user", "content": simple_prompt},
                ]
                response = await local_provider.chat(
                    messages=messages,
                    temperature=0.8,
                    max_tokens=256,  # 4096 ctx — balanced for quality+stability
                )
                if response and not response.error and response.text:
                    reply_text = response.text[:COMMENT_MAX_CHARS]
                    await message.reply(reply_text)
            except Exception as e:
                logger.debug(f"Group photo comment error: {e}")
        else:
            logger.debug("Local model not available for group photo comment — skipping")
        return

    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    # Get the highest resolution photo
    photo: PhotoSize = message.photo[-1]

    # Get caption or default prompt
    caption = message.caption or ""
    if caption:
        prompt = caption
    else:
        prompt = (
            "Рассмотри это фото МАКСИМАЛЬНО внимательно и подробно:\n\n"
            "1. Если на фото АВТОМОБИЛЬ — определи: марку, модель, поколение, год, тип кузова, "
            "цвет, состояние. Укажи ориентировочную стоимость на вторичном рынке.\n\n"
            "2. Если на фото ЗАПЧАСТЬ — определи: что это за деталь, для какого авто подходит, "
            "артикул (OEM-номер), если виден. Посоветуй где купить и примерную цену.\n\n"
            "3. Если на фото ДОКУМЕНТ на авто (ПТС, СТС, диагностическая карта, страховка) — "
            "считай ВСЕ данные: VIN, марку, модель, год, двигатель, мощность, объём, "
            "тип кузова, цвет, номер кузова. "
            "НИКОГДА не показывай ФИО владельца и адрес — это персональные данные! "
            "Покажи только технические данные.\n\n"
            "4. Если на фото ЭКРАН СКАНЕРА OBD-II — считай коды ошибок и расшифруй их.\n\n"
            "5. Если на фото ПОВРЕЖДЕНИЕ/ПОЛОМКА — опиши что видишь, возможные причины, "
            "что делать и примерную стоимость ремонта.\n\n"
            "6. Если что-то другое — просто опиши что видишь.\n\n"
            "Пиши живо и заботливо, как девушка-автоэксперт."
        )

    # Build extra context
    extra_context_parts = []
    user_context = _get_user_persona_context(message)
    if user_context:
        extra_context_parts.append(user_context)

    # Add user car context if available
    try:
        user_cars = await get_user_cars(message.from_user.id)
        if user_cars:
            car_lines = ["Машины пользователя:"]
            for car in user_cars[:3]:
                car_line = f"- {car['brand']} {car['model']}"
                if car['year']:
                    car_line += f" {car['year']}"
                if car['vin']:
                    car_line += f", VIN: {car['vin']}"
                car_lines.append(car_line)
            extra_context_parts.append("\n".join(car_lines))
    except Exception:
        pass

    # Download the photo and convert to base64
    try:
        file_info = await message.bot.get_file(photo.file_id)
        if not file_info or not file_info.file_path:
            await message.answer("Не удалось скачать фото 😅 Попробуй ещё раз")
            return

        file_url = f"https://api.telegram.org/file/bot{config.BOT_TOKEN}/{file_info.file_path}"

        import httpx
        async with httpx.AsyncClient(timeout=30.0) as client:
            dl_response = await client.get(file_url)
            if dl_response.status_code == 200:
                image_base64 = base64.b64encode(dl_response.content).decode("utf-8")

                # Determine media type
                media_type = "image/jpeg"
                if file_info.file_path.endswith(".png"):
                    media_type = "image/png"
                elif file_info.file_path.endswith(".webp"):
                    media_type = "image/webp"

                extra_context = "\n\n".join(extra_context_parts) if extra_context_parts else ""

                ai_response = await ai_router.analyze_image(
                    user_id=message.from_user.id,
                    image_base64=image_base64,
                    prompt=prompt,
                    extra_context=extra_context,
                )

                if ai_response.error or not ai_response.text:
                    await message.answer("Ой, не получилось разглядеть фото 😅 Попробуй ещё раз!")
                    return

                reply_text = ai_response.text
                reply_text = _clean_markdown(reply_text)

                # CRITICAL: Replace any plain partner URLs with affiliate goto_link
                reply_text = _replace_plain_urls_with_affiliate(reply_text)

                # Collect partner links for photo responses
                photo_partner_links = []

                # Check if AI found a VIN in the photo — suggest partner links for parts
                detected_vin = _detect_vin(reply_text)
                if detected_vin and len(detected_vin) == 17:
                    try:
                        links = partner_manager.get_all_relevant_links(reply_text, max_programs=5)
                        if links:
                            for link in links:
                                photo_partner_links.append((link['name'], link['url']))
                    except Exception as e:
                        logger.debug(f"Partner links from photo error: {e}")

                # Check if AI found part numbers — suggest partner links
                detected_parts = extract_part_numbers(reply_text)
                if detected_parts:
                    try:
                        links = partner_manager.get_all_relevant_links(reply_text, max_programs=5)
                        if links:
                            for link in links:
                                if (link['name'], link['url']) not in photo_partner_links:
                                    photo_partner_links.append((link['name'], link['url']))
                    except Exception as e:
                        logger.debug(f"Partner links from photo parts error: {e}")

                # Clean raw affiliate URLs from AI text (same as _send_response does)
                if photo_partner_links:
                    reply_text = _clean_raw_partner_urls(reply_text, photo_partner_links)
                    partner_section = _format_partner_links_section(photo_partner_links)
                    if partner_section:
                        reply_text = reply_text.rstrip() + "\n\n" + partner_section

                # Split if too long
                if len(reply_text) <= config.TELEGRAM_TEXT_LIMIT:
                    await message.answer(reply_text)
                else:
                    chunks = _split_message(reply_text, max_length=config.TELEGRAM_TEXT_LIMIT)
                    for chunk in chunks:
                        await message.answer(chunk)
                return
            else:
                await message.answer("Не удалось скачать фото 😅 Попробуй ещё раз")
                return

    except Exception as e:
        logger.error(f"Photo processing error: {e}")
        await message.answer("Ой, что-то пошло не так с фото 😅 Напиши текстом, попробую помочь!")


# ── Voice message handler ─────────────────────────────────────────────────────

@chat_router.message(F.voice)
async def handle_voice(message: Message):
    """Handle voice messages — transcribe and process."""
    if not await _check_user(message):
        return

    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    await message.answer("Слушаю... 🎧")

    voice = message.voice
    text = await process_voice_message(message.bot, voice.file_id)

    if text and not text.startswith("Не удалось"):
        await _process_text_message(message, text)
    else:
        await message.answer(text)


# ── Main text message handler ─────────────────────────────────────────────────

@chat_router.message(F.text)
async def handle_text(message: Message):
    """Handle text messages — main interaction point."""
    if not await _check_user(message):
        return

    text = message.text.strip()
    if not text:
        return

    # ── Guard: unknown commands should NOT be routed to AI ──
    # If a message starts with '/' but no admin/chat handler matched it,
    # it means the command is either unknown or the user is not admin.
    # Sending it to the AI would produce a confusing AI-generated response
    # (e.g. "/shop_status" → "Лёха поставил машину на ремонт...").
    # Instead, show a brief help message.
    if text.startswith("/"):
        # Extract command name (first word, strip @bot_mention suffix)
        cmd = text.split()[0].lower().split("@")[0]
        # List of known commands that SHOULD have been handled by now
        known_cmds = {
            "/start", "/help", "/app", "/clear", "/diagnostic", "/parts",
            "/normal", "/mycar", "/delcar", "/mileage",
        }
        if cmd not in known_cmds:
            # Unknown command — don't send to AI, show help instead
            await message.answer(
                f"Не знаю команду {cmd}. Напиши /help — покажу что умею. "
                f"Если нужна подборка товаров — /selection"
            )
            return

    await _process_text_message(message, text)


async def _process_text_message(message: Message, text: str):
    """Core message processing with AI, search, diagnostics, parts, VIN, and personalization.
    
    Has an OVERALL TIMEOUT of 45 seconds to prevent the user from waiting indefinitely
    when all AI providers and searches fail slowly.

    v5.1 — adds REQUEST DEDUPLICATION: if the same user sends the same message
    within 3 seconds (mobile double-tap, network retry), return the cached
    response instead of re-processing.
    """
    import asyncio
    user_id = message.from_user.id

    # ── v5.1: Request deduplication (3-second window) ──
    # Mobile users often double-tap Send, and Telegram sometimes delivers
    # the same message twice within 1-2 seconds. Processing both wastes AI
    # tokens and produces duplicate replies.
    dedup = get_request_deduplicator()
    cached_response = dedup.check(user_id, text)
    if cached_response:
        # Send the cached response directly and skip processing
        try:
            await message.answer(cached_response)
        except Exception:
            pass
        logger.info(f"Returning deduplicated response for user {user_id}")
        return

    chat_mode = await get_chat_mode(user_id)

    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    # ── OVERALL TIMEOUT: Max 90 seconds for the entire processing ──
    # INCREASED from 60s — local model generation can take 10-30s,
    # plus web search ~10-15s, plus cloud fallback ~15-30s.
    # 60s was too tight when local model + cloud fallback both run.
    _OVERALL_TIMEOUT = 90.0  # seconds

    async def _do_process():
        await _process_text_message_inner(message, text, user_id, chat_mode)

    try:
        await asyncio.wait_for(_do_process(), timeout=_OVERALL_TIMEOUT)
    except asyncio.TimeoutError:
        logger.error(f"Overall timeout ({_OVERALL_TIMEOUT}s) for user {user_id}")
        await message.answer(
            "Ой, я немного застряла 🙈 Давай попробуем ещё раз? "
            "Иногда мне нужно чуть-чуть времени, чтобы собраться с мыслями"
        )


async def _process_text_message_inner(message: Message, text: str, user_id: int, chat_mode: str):
    """Inner processing logic — called with timeout wrapper.
    
    v2.0 CONCURRENT SEARCH + AI: Web searches run IN PARALLEL with the AI call.
    This eliminates the 20-40 second delay caused by sequential search→AI flow.
    The AI response arrives first, then we enhance it with search results if available.
    """

    # Send a "thinking" status message so the user knows we're working
    text_lower = text.lower()
    if any(kw in text_lower for kw in ["запчаст", "деталь", "артикул", "купить", "найти запчас", "подобрать", "vin", "вин"]):
        thinking_msg = random.choice(ASYA_PHRASES["part_search"])
    elif any(kw in text_lower for kw in ["стучит", "не работает", "горит", "ошибка", "чек", "перегрев", "не заводит", "троит", "вибрац"]):
        thinking_msg = random.choice(ASYA_PHRASES["diagnostic_start"])
    else:
        thinking_msg = random.choice(ASYA_PHRASES["thinking"])
    status_msg = await message.answer(thinking_msg)

    # ── Build extra context ────────────────────────────────────────────────

    extra_context_parts = []
    collected_partner_links = []  # List of (name, url) tuples — for clean formatting after AI response

    # 0. v5.1: Chat type context — tell the AI what kind of chat it's in
    # so it can adapt tone, length, and style (private vs group vs forum).
    try:
        is_own_channel_check = (
            str(message.chat.id) == str(config.CHANNEL_ID)
            or message.chat.username == config.CHANNEL_ID.replace("@", "")
        )
        chat_ctx = chat_type_context(
            chat_type=message.chat.type,
            is_own_channel=is_own_channel_check,
            is_inline=False,
        )
        if chat_ctx:
            extra_context_parts.append(chat_ctx)
    except Exception as e:
        logger.debug(f"chat_type_context error: {e}")

    # 0. User persona context for personalized communication
    user_context = _get_user_persona_context(message)
    if user_context:
        extra_context_parts.append(user_context)

    # Interbot removed — each bot works independently

    # 0.5. User car profile context — so Asya knows their cars
    try:
        user_cars = await get_user_cars(user_id)
        if user_cars:
            car_lines = ["Машины пользователя:"]
            for car in user_cars[:3]:
                car_line = f"- {car['brand']} {car['model']}"
                if car['year']:
                    car_line += f" {car['year']}"
                if car['engine']:
                    car_line += f", двигатель: {car['engine']}"
                if car['mileage']:
                    car_line += f", пробег: {car['mileage']} км"
                if car['vin']:
                    car_line += f", VIN: {car['vin']}"
                car_lines.append(car_line)
            extra_context_parts.append("\n".join(car_lines))
    except Exception as e:
        logger.debug(f"Error loading user cars: {e}")

    # 1. Detect VIN code or body number
    vin_code = _detect_vin(text)
    body_number = _detect_body_number(text) if not vin_code else None
    is_vin_query = bool(vin_code) or bool(body_number) or _is_vin_query(text)

    if is_vin_query:
        vin_or_body = vin_code or body_number or text.strip()
        
        # Add primary partner links (Rossko, Autopiter RU, AvtoALL)
        primary_links_context = ""
        try:
            primary_links_context = partner_manager.format_primary_parts_links()
        except Exception as e:
            logger.debug(f"Primary links context error: {e}")
        
        all_context = extra_context_parts.copy()
        if primary_links_context:
            all_context.append(primary_links_context)
        
        # Run VIN web search CONCURRENTLY with AI call for speed
        import asyncio as _asyncio_vin
        
        async def _vin_search_task():
            if not vin_code or len(vin_code) != 17:
                return ""
            try:
                search_query = f"VIN {vin_code} расшифровка автомобиль характеристики"
                results = await _asyncio_vin.wait_for(web_search(search_query, max_results=3), timeout=5.0)
                if results:
                    return "Результаты поиска по VIN:\n" + format_search_results(results, max_items=3)
            except _asyncio_vin.TimeoutError:
                logger.debug(f"VIN web search timed out")
            except Exception as e:
                logger.debug(f"VIN web search error: {e}")
            return ""
        
        # Start VIN search and AI call concurrently
        search_task = _asyncio_vin.create_task(_vin_search_task())
        ai_task = _asyncio_vin.create_task(
            ai_router.decode_vin(
                user_id=user_id,
                vin_code=vin_or_body,
                extra_context="\n".join(all_context),
            )
        )
        
        # Wait for AI (primary), collect search result when available
        response = await ai_task
        try:
            vin_search_context = await _asyncio_vin.wait_for(search_task, timeout=3.0)
        except _asyncio_vin.TimeoutError:
            vin_search_context = ""
        
        # If AI failed but search succeeded, retry with search context
        if response.error and vin_search_context:
            all_context.append(vin_search_context)
            response = await ai_router.decode_vin(
                user_id=user_id,
                vin_code=vin_or_body,
                extra_context="\n".join(all_context),
            )
        # Collect VIN partner links for clean formatting (cross-category)
        vin_partner_links = []
        try:
            all_links_data = partner_manager.get_all_relevant_links(vin_or_body, max_programs=5)
            for pl in all_links_data:
                vin_partner_links.append((pl['name'], pl['url']))
        except Exception:
            # Fallback to primary parts links only
            try:
                primary_links_data = partner_manager.get_primary_parts_links()
                for pl in primary_links_data:
                    vin_partner_links.append((pl['name'], pl['url']))
            except Exception:
                pass
        await _send_response(message, response, status_msg, vin_partner_links)
        return

    # 2. Detect car brand
    try:
        brand = identify_car_brand(text)
    except Exception as e:
        logger.debug(f"identify_car_brand error: {e}")
        brand = None
    if brand:
        from bot.asya import get_brand_info
        info = get_brand_info(brand)
        if info:
            extra_context_parts.append(f"Упомянута марка: {brand} ({info['country']}, холдинг: {info['parent']})")

    # 3. Detect OBD-II codes
    obd_codes = detect_obd2_codes(text)
    if obd_codes:
        for code in obd_codes:
            desc = lookup_obd2_code(code)
            if desc:
                extra_context_parts.append(f"Код ошибки {code}: {desc}")
        # NOTE: OBD code web searches are now done CONCURRENTLY with AI (see below)

    # 4. Detect part numbers — NO MORE catalog searches, give partner links instead
    part_numbers = extract_part_numbers(text)
    is_part_query = bool(part_numbers) or is_part_number(text.strip()) or chat_mode == "parts"

    # Always add primary partner links for parts/VIN queries
    if is_part_query:
        try:
            primary_links = partner_manager.format_primary_parts_links()
            if primary_links:
                extra_context_parts.append(primary_links)
        except Exception as e:
            logger.debug(f"Primary links error: {e}")

    # 5. Detect car symptoms
    symptoms = detect_symptoms(text)
    is_diagnostic = bool(symptoms) or chat_mode == "diagnostic"

    if symptoms:
        diag_context = build_diagnostic_context(text)
        if diag_context:
            extra_context_parts.append(diag_context)

    # 6. Web search — triggers defined, but searches run CONCURRENTLY with AI (see below)
    needs_search = (
        is_diagnostic or
        is_part_query or
        any(kw in text.lower() for kw in [
            "найди", "поиск", "ищи", "где купить", "сколько стоит",
            "новости", "что нового", "обзор", "сравни", "лучший",
            "рекомендуй", "посоветуй", "купить", "заказать",
            "когда", "где", "какой", "какая", "какие",
            "запчаст", "деталь", "артикул", "оригинал", "аналог",
            "замена", "ремонт", "поломк", "стучит", "не работает",
            "горит", "ошибка", "код", "чек", "check",
            "цена", "стоимость", "подбор", "купить",
            "отзыв", "проблем", "бренд", "производител",
            "характеристик", "мощност", "расход", "масло",
        ])
    )
    
    # Build search query (but don't execute yet — will run in parallel with AI)
    search_query = None
    if needs_search:
        search_query = text
        if brand:
            search_query = f"{brand} {text}"
        text_lower_local = text.lower().strip()
        _SEARCH_QUERY_REWRITES = {
            "какие новости": "автомобильные новости сегодня 2026",
            "что нового": "автоновости сегодня 2026",
            "новости": "автомобильные новости сегодня 2026",
            "что случилось": "автомобильные новости сегодня",
            "что происходит": "автомобильный рынок новости",
            "какие новости сегодня": "автомобильные новости сегодня 2026",
            "что нового в авт мире": "автомобильные новинки 2026",
            "что нового на рынке": "авторынок новости 2026",
        }
        for vague, specific in _SEARCH_QUERY_REWRITES.items():
            if vague in text_lower_local and len(text_lower_local) < len(vague) + 15:
                search_query = specific
                if brand:
                    search_query = f"{brand} {specific}"
                break

    # 6.5. Spare part query — give partner links instead of searching catalogs
    is_spare_part_query = (
        any(kw in text.lower() for kw in [
            "запчаст", "деталь", "артикул", "купить запчас", "купить детал",
            "оригинал", "аналог", "замена", "подбор", "номер детал",
            "oem", "оригинальн", "цена", "стоимость", "скольк",
            "колодки", "фильтр", "свечи", "ремень", "амортизатор",
            "подшипник", "сальник", "прокладк", "датчик", "реле",
            "насос", "стойка", "шаровая", "наконечник", "сцепление",
            "где купить", "подобрать", "найти запчас",
        ])
        or is_part_number(text.strip())
        or bool(part_numbers)
        or chat_mode == "parts"
    )

    # Add primary partner links (Rossko, Autopiter RU, AvtoALL) for ANY spare part query
    if is_spare_part_query:
        try:
            primary_links = partner_manager.format_primary_parts_links()
            if primary_links and primary_links not in extra_context_parts:
                extra_context_parts.append(primary_links)
            # Collect ALL relevant partner links for post-processing (clean formatting)
            # Use get_all_relevant_links for cross-category coverage
            try:
                all_links_data = partner_manager.get_all_relevant_links(text, max_programs=5)
                for pl in all_links_data:
                    collected_partner_links.append((pl['name'], pl['url']))
            except Exception:
                # Fallback to primary parts links only
                try:
                    primary_links_data = partner_manager.get_primary_parts_links()
                    for pl in primary_links_data:
                        collected_partner_links.append((pl['name'], pl['url']))
                except Exception:
                    pass
        except Exception as e:
            logger.debug(f"Primary links for spare parts error: {e}")

    # 7. Partner program context — ALL relevant partner links for all topics
    # Use get_all_relevant_links for cross-category coverage (not just autoparts).
    # This ensures tires, insurance, tools, checkauto links are included when relevant.
    try:
        partner_context = partner_manager.generate_partner_context(text)
        if partner_context:
            extra_context_parts.append(partner_context)
        # Also collect cross-category partner links for clean formatting
        if not is_spare_part_query:  # Already collected above for spare part queries
            try:
                all_relevant = partner_manager.get_all_relevant_links(text, max_programs=5)
                for pl in all_relevant:
                    collected_partner_links.append((pl['name'], pl['url']))
            except Exception:
                pass
    except Exception as e:
        logger.error(f"Partner context error: {e}")

    # ── Route to AI — CONCURRENT with web search ──────────────────────────
    # KEY FIX: Start AI call and web searches IN PARALLEL.
    # Previously, web_search() was called SEQUENTIALLY before AI, adding 5-20s delay.
    # Now both run concurrently, so AI responds fast and search results are used
    # only if AI fails (as retry context) or for next conversation turn.

    extra_context = "\n\n".join(extra_context_parts) if extra_context_parts else ""

    # Determine route type based on chat context
    is_group_chat = message.chat.type in ("group", "supergroup")
    is_own_channel = str(message.chat.id) == str(config.CHANNEL_ID)

    import asyncio

    # Define async search tasks
    async def _run_web_search():
        """Run web search with strict timeout. Returns search context string or empty."""
        if not search_query:
            return ""
        try:
            results = await asyncio.wait_for(
                web_search(search_query, max_results=3),
                timeout=6.0,
            )
            if results:
                return "Результаты поиска:\n" + format_search_results(results, max_items=3)
        except asyncio.TimeoutError:
            logger.warning(f"Web search timed out for query: {search_query[:50]}")
        except Exception as e:
            logger.error(f"Web search error: {e}")
        return ""

    async def _run_obd_search():
        """Run OBD code searches concurrently. Returns context string or empty."""
        if not obd_codes:
            return ""
        parts = []
        for code in obd_codes[:2]:
            try:
                code_info = await asyncio.wait_for(
                    search_diagnostic_code(code),
                    timeout=5.0,
                )
                if code_info.get("links"):
                    links_text = "\n".join(
                        f"- {l['title']}: {l['url']}" for l in code_info["links"][:3]
                    )
                    parts.append(f"Подробности по ошибке {code}:\n{links_text}")
            except asyncio.TimeoutError:
                logger.debug(f"OBD code search timed out: {code}")
            except Exception as e:
                logger.debug(f"OBD code search error: {e}")
        return "\n".join(parts) if parts else ""

    # Start search tasks concurrently
    search_tasks = []
    if search_query:
        search_tasks.append(asyncio.create_task(_run_web_search()))
    if obd_codes:
        search_tasks.append(asyncio.create_task(_run_obd_search()))

    # Launch AI call
    if is_diagnostic:
        ai_coro = ai_router.diagnose_car(
            user_id=user_id,
            symptoms=text,
            extra_context=extra_context,
        )
    elif is_part_query:
        ai_coro = ai_router.find_spare_part(
            user_id=user_id,
            article=part_numbers[0] if part_numbers else text.strip(),
            part_info=extra_context,
        )
    elif is_group_chat and not is_own_channel:
        # GROUP/SUPERGROUP (not our channel) → LOCAL MODEL PREFERRED, CLOUD FALLBACK
        # v5.0: Also collect partner links based on the message text — so even
        # in group comments Ася can naturally suggest relevant partners
        # (autoparts, tires, rental, insurance, etc.) using the proper
        # goto_link from partners.json.
        try:
            group_partner_links_data = partner_manager.get_all_partner_links_for_dialog(
                text, max_programs=3
            )
            for pl in group_partner_links_data:
                collected_partner_links.append((pl['name'], pl['url']))
        except Exception as e:
            logger.debug(f"Group partner links collection error: {e}")

        from ai.router import ai_router as _ar
        local_provider = _ar._local
        if local_provider and await local_provider.is_available():
            # Build group comment prompt — include partner context if available
            group_user_content = text[:500]
            if collected_partner_links:
                partner_hint = "\n\nПартнёрские ссылки (используй КАК ЕСТЬ, если уместно):\n"
                for name, url in collected_partner_links[:3]:
                    partner_hint += f"- {name}: {url}\n"
                partner_hint += "Вставь ОДНУ ссылку естественно, если это к месту. Не перечисляй все."
                group_user_content = group_user_content + partner_hint

            group_messages = [
                {"role": "system", "content": "Ты Ася — автоэксперт. Пиши короткие комментарии до 300 символов. Живо и естественно. Без markdown. Без политики. Без рекламы канала."},
                {"role": "user", "content": group_user_content},
            ]
            try:
                local_response = await local_provider.chat(
                    messages=group_messages,
                    temperature=0.8,
                    max_tokens=512,  # 4096 ctx — balanced for quality group responses
                )
                if local_response and not local_response.error and local_response.text:
                    from ai.providers.base import AIResponse
                    # Cancel search tasks since we're done fast
                    for st in search_tasks:
                        st.cancel()
                    response = AIResponse(
                        text=local_response.text[:COMMENT_MAX_CHARS],
                        model="local-qwen3-4b",
                        provider="local",
                    )
                    await _send_response(message, response, status_msg, collected_partner_links)
                    return
                else:
                    logger.debug("Local model failed for group comment — trying cloud fallback")
            except Exception as e:
                logger.debug(f"Local model group comment error: {e}")
        # Cloud fallback for groups
        ai_coro = ai_router.chat(
            user_id=user_id,
            message=text,
            extra_context="Отвечай коротко, до 300 символов. Живо и естественно. Без политики.",
        )
    else:
        ai_coro = ai_router.chat(
            user_id=user_id,
            message=text,
            extra_context=extra_context,
        )

    # Run AI call — search tasks are already running in parallel
    response = await ai_coro

    # Collect any search results that completed (best-effort, don't wait long)
    search_context = ""
    for st in search_tasks:
        try:
            if not st.done():
                result = await asyncio.wait_for(st, timeout=2.0)
            else:
                result = st.result()
            if result:
                search_context += result + "\n"
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        except Exception:
            pass

    # If AI failed AND we have search results, retry AI with search context
    if response.error and search_context:
        logger.info("AI failed on first attempt, retrying with search context")
        enhanced_context = extra_context + "\n\n" + search_context if extra_context else search_context
        if is_diagnostic:
            response = await ai_router.diagnose_car(
                user_id=user_id,
                symptoms=text,
                extra_context=enhanced_context,
            )
        elif is_part_query:
            response = await ai_router.find_spare_part(
                user_id=user_id,
                article=part_numbers[0] if part_numbers else text.strip(),
                part_info=enhanced_context,
            )
        else:
            response = await ai_router.chat(
                user_id=user_id,
                message=text,
                extra_context=enhanced_context,
            )

    await _send_response(message, response, status_msg, collected_partner_links)


def _smart_truncate(text: str, max_chars: int) -> str:
    """Truncate text to max_chars, trying not to cut in the middle of a sentence.
    
    Looks for the last sentence boundary (. ! ? …) within the limit.
    Falls back to last newline, then last space, then hard cut.
    Always appends "..." if truncated.
    """
    if len(text) <= max_chars:
        return text

    # Reserve 3 chars for "..."
    limit = max_chars - 3
    if limit <= 0:
        return "..."

    # Try to find last sentence boundary within limit
    sentence_endings = ['. ', '! ', '? ', '… ', '.\n', '!\n', '?\n', '…\n']
    best_pos = -1
    best_ending_len = 0
    for ending in sentence_endings:
        pos = text.rfind(ending, 0, limit + 1)
        if pos > best_pos:
            best_pos = pos
            best_ending_len = len(ending)

    if best_pos > limit // 2:
        # Found a good sentence boundary — cut after the punctuation mark
        return text[:best_pos + best_ending_len].rstrip() + "..."

    # Try last newline
    nl_pos = text.rfind('\n', 0, limit + 1)
    if nl_pos > limit // 2:
        return text[:nl_pos].rstrip() + "..."

    # Try last space
    sp_pos = text.rfind(' ', 0, limit + 1)
    if sp_pos > limit // 2:
        return text[:sp_pos].rstrip() + "..."

    # Hard cut
    return text[:limit].rstrip() + "..."


async def _send_response(message: Message, response, status_msg=None, partner_links=None):
    """Send AI response to user, handling errors and length limits.
    
    If partner_links is provided (list of (name, url) tuples), appends a cleanly
    formatted section with named links after the AI response text.

    v5.0: For forum supergroups (topics), replies are sent in the same
    message_thread_id so the bot's response stays in the correct topic.
    For comment threads on a channel post, replies go to the same thread.
    """
    # Delete the "thinking" status message
    if status_msg:
        try:
            await status_msg.delete()
        except Exception:
            pass

    if response.error:
        logger.error(f"AI error: {response.error_message}")
        await message.answer(
            "Ой, зависла немного 😅 Дай мне минутку и попробуй ещё раз!"
        )
        return

    reply_text = response.text

    # Ensure Asya doesn't use markdown formatting in chat
    reply_text = _clean_markdown(reply_text)

    # CRITICAL: Replace any plain partner URLs (rossko.ru, autopiter.ru, etc.)
    # with affiliate goto_link equivalents from partners.json
    reply_text = _replace_plain_urls_with_affiliate(reply_text)

    # Remove raw affiliate URLs that AI may have dumped into the response
    if partner_links:
        reply_text = _clean_raw_partner_urls(reply_text, partner_links)

    # Append cleanly formatted partner links section
    if partner_links:
        partner_section = _format_partner_links_section(partner_links)
        if partner_section:
            reply_text = reply_text.rstrip() + "\n\n" + partner_section

    # ── Enforce character limits based on chat type ──
    # v5.1: Use ADAPTIVE limits — short user msg → short reply, long user msg
    # → long reply. Falls back to base limits if adaptive fails.
    is_group_chat = message.chat.type in ("group", "supergroup")
    is_own_channel = (
        str(message.chat.id) == str(config.CHANNEL_ID)
        or message.chat.username == config.CHANNEL_ID.replace("@", "")
    )

    # Determine the original user message text (for adaptive sizing)
    user_text_for_adaptive = ""
    try:
        # message.text holds the user's text; fall back to message.caption for photos
        user_text_for_adaptive = message.text or message.caption or ""
    except Exception:
        pass

    try:
        # v5.1: Adaptive limit based on user message length + chat type
        limit = adaptive_max_chars(
            user_message=user_text_for_adaptive,
            chat_type=message.chat.type,
            is_own_channel=is_own_channel,
        )
    except Exception:
        # Fallback to old fixed limits
        if message.chat.type == "private":
            limit = CHAT_MAX_CHARS
        elif not is_own_channel and is_group_chat:
            limit = COMMENT_MAX_CHARS
        else:
            limit = GROUP_MAX_CHARS

    if len(reply_text) > limit:
        logger.info(f"Truncating response: {len(reply_text)} → {limit} chars (chat_type={message.chat.type})")
        reply_text = _smart_truncate(reply_text, limit)

    # v5.0: Determine the correct message_thread_id for forum/topic replies.
    # - In forum supergroups, every topic has a thread_id (message.message_thread_id)
    # - In channel comment threads, the thread_id is also set
    # - reply_to_message_id is kept when the user replied to a specific message
    #   (so the bot's reply is properly nested in the comment thread)
    reply_kwargs = {}
    if hasattr(message, "message_thread_id") and message.message_thread_id:
        reply_kwargs["message_thread_id"] = message.message_thread_id
    # If the user's message was itself a reply in a thread, reply TO that message
    # so the bot's response is nested under the same parent in Telegram's UI.
    if (getattr(message, "reply_to_message", None) and
            getattr(message.reply_to_message, "message_id", None)):
        # Only set reply_to_message_id if we're in a forum/group/channel — never in private
        # (in private chats it's just noise).
        if message.chat.type != "private":
            reply_kwargs["reply_to_message_id"] = message.reply_to_message.message_id

    # Split long messages (Telegram limit 4096 chars)
    # Plain text — no HTML parse mode needed since partner links are plain text
    if len(reply_text) <= config.TELEGRAM_TEXT_LIMIT:
        await message.answer(reply_text, **reply_kwargs)
    else:
        chunks = _split_message(reply_text, max_length=config.TELEGRAM_TEXT_LIMIT)
        for chunk in chunks:
            await message.answer(chunk, **reply_kwargs)

    # ── v5.1: Record this response in dedup cache ──
    # If the user sends the same message within 3 seconds (mobile double-tap,
    # network retry), we return this cached response instead of re-processing.
    try:
        if user_text_for_adaptive and reply_text:
            dedup = get_request_deduplicator()
            dedup.record(message.from_user.id, user_text_for_adaptive, reply_text)
    except Exception:
        pass


# ── Utility functions ──────────────────────────────────────────────────────────

# Shop icon mapping for clean link formatting (v5.0 — expanded)
_SHOP_ICONS = {
    "rossko": "🔧",
    "autopiter": "🔍",
    "avtoall": "🛒",
    "exist": "📋",
    "emex": "🔩",
    "autodoc": "🚗",
    "zzap": "💰",
    "ixora": "⚙️",
    # Tires
    "bs-tyres": "🛞",
    "euro-diski": "🛞",
    "koleso": "🛞",
    "колесо": "🛞",
    # Insurance
    "petrolplus": "🛡️",
    # Check auto
    "avtocod": "🔍",
    # Car rental
    "discovercars": "🚗",
    "localrent": "🚗",
    # Marketplaces
    "aliexpress": "🛒",
    "alibaba": "🛒",
    "geekbuying": "🛒",
    "raketa": "🛒",
    # Travel
    "aviasales": "✈️",
    "авиасейлс": "✈️",
    "globalyo": "📱",
    "global yo": "📱",
    # Education
    "skyeng": "🎓",
    "real-avto": "🎓",
    "автошкола": "🎓",
    # Misc
    "globaldrive": "🔧",
    "mirdvornikov": "🔧",
    "hyperauto": "🔧",
    "lukoil": "🔧",
    "xistore": "🔧",
}
_DEFAULT_SHOP_ICON = "🔗"


def _get_icon_for_partner_name(name: str) -> str:
    """Get the right emoji icon for a partner based on its name (v5.0)."""
    name_lower = name.lower()
    for key, icon in _SHOP_ICONS.items():
        if key in name_lower:
            return icon
    return _DEFAULT_SHOP_ICON


def _format_partner_links_section(partner_links: list) -> str:
    """Format partner links as a clean, readable section with plain text URLs.
    
    v5.0 — uses per-category icons (🔧 parts, 🛞 tires, 🛡️ insurance,
    🚗 rental, 🛒 marketplaces, ✈️ travel, 🎓 education) so each link
    visually communicates its category.

    Takes a list of (name, url) tuples and returns a nicely formatted
    string. The icon is picked based on the partner name.
    
    Result looks like:
    
    Где купить:
    🔧 Росско — https://ujhjj.com/g/...
    🔍 Autopiter — https://rcpsj.com/g/...
    🛒 AvtoALL — https://sgkaa.com/g/...
    """
    if not partner_links:
        return ""
    
    lines = ["Где купить:"]
    for name, url in partner_links[:5]:
        icon = _get_icon_for_partner_name(name)
        lines.append(f'{icon} {name} — {url}')
    
    return "\n".join(lines)


def _clean_raw_partner_urls(text: str, partner_links: list) -> str:
    """Remove raw affiliate URLs that AI may have dumped into the response.
    
    If the AI already included the full affiliate URLs in its response text,
    this function removes them to avoid duplication with the clean formatted
    section that will be appended by _format_partner_links_section.
    Also removes shop name + URL patterns like "Росско: https://...".
    """
    if not partner_links:
        return text
    
    for name, url in partner_links:
        url_escaped = re.escape(url)
        # Remove standalone URL lines
        text = re.sub(
            rf'^[\s\-—]*{url_escaped}[\s]*$',
            '',
            text,
            flags=re.MULTILINE
        )
        # Remove "name: URL" or "name — URL" patterns on their own lines
        text = re.sub(
            rf'^[\s\-—]*{re.escape(name)}\s*[:\-—]\s*{url_escaped}[\s]*$',
            '',
            text,
            flags=re.MULTILINE
        )
        # Remove any line containing the full raw affiliate URL (even mid-sentence)
        # This catches cases where AI writes: "Посмотри на Росско: https://ujhjj.com/g/..."
        text = re.sub(
            rf'[^\n]*{url_escaped}[^\n]*',
            '',
            text,
            flags=re.MULTILINE
        )
        # Remove bare URL anywhere in text (affiliate URLs are very long, easy to detect)
        if len(url) > 50:  # Only long tracking URLs
            # Match the base tracking domain pattern
            url_base = re.escape(url.split('?')[0])  # Before query params
            text = re.sub(
                rf'{url_base}\?[^\s\n"]+',
                '',
                text
            )
    
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


# Known partner domains that MUST use affiliate links, not plain URLs
# v5.0 EXPANDED — covers ALL 25 partners from partners.json
_PARTNER_DOMAINS_MAP = {
    # Auto parts
    "rossko.ru": "Росско",
    "autopiter.ru": "Autopiter",
    "autopiter.kz": "Autopiter KZ",
    "avtoall.ru": "AvtoALL",
    "globaldrive.ru": "Globaldrive",
    "mirdvornikov.ru": "МирДворников",
    "hyperauto.ru": "Hyperauto",
    "lukoil-shop.com": "Лукойл",
    "lukoil-shop.ru": "Лукойл",
    "xistore.by": "Xistore",
    # Tires
    "bs-tyres.ru": "BS-Tyres",
    "euro-diski.ru": "Euro-diski",
    "koleso.ru": "Колесо",
    # Insurance
    "petrolplus.ru": "PetrolPlus",
    # Check auto
    "avtocod.ru": "Avtocod",
    # Car rental
    "discovercars.com": "DiscoverCars",
    "localrent.com": "Localrent",
    # Marketplaces
    "aliexpress.ru": "AliExpress RU",
    "aliexpress.com": "AliExpress WW",
    "alibaba.com": "Alibaba",
    "geekbuying.com": "Geekbuying",
    "raketacn.ru": "RAKETA",
    # Travel
    "aviasales.ru": "Авиасейлс",
    "globalyo.com": "Global YO",
    # Education
    "skyeng.ru": "Skyeng",
    "real-avto.com": "Автошкола РЕАЛ",
    # Legacy (kept for backward compat)
    "exist.ru": "Exist",
    "emex.ru": "Emex",
    "autodoc.ru": "Autodoc",
    "zzap.ru": "Zzap",
}


def _replace_plain_urls_with_affiliate(text: str) -> str:
    """Replace any plain partner domain URLs with affiliate goto_link equivalents.
    
    When the AI generates responses containing plain URLs like rossko.ru or 
    autopiter.ru instead of the affiliate tracking links from partners.json,
    this function detects them and replaces with the proper goto_link.

    This handles cases where:
    - AI ignores the system prompt and invents plain URLs
    - AI uses domain names without the affiliate wrapper
    - Photo handler bypasses the normal link injection pipeline

    v5.1 — uses PRECOMPILED regexes (find_full_urls / find_bare_domains)
    instead of re.compile per call. Saves ~30-50ms per call.
    """
    try:
        partner_manager.ensure_loaded()
    except Exception:
        return text
    
    for domain, display_name in _PARTNER_DOMAINS_MAP.items():
        prog = partner_manager.get_by_site(domain)
        if not prog or not prog.goto_link:
            continue
        
        affiliate_url = prog.goto_link
        
        # Pattern 1: Full URLs with paths — https://rossko.ru/search?text=abc
        # Replace the entire URL with the affiliate link
        # v5.1: Use precompiled regex from optimizations module
        try:
            matches = find_full_urls(text, domain)
        except Exception:
            # Fallback to inline regex if precompile wasn't done
            pattern = rf'https?://{re.escape(domain)}[^\s<>)\]"\']*'
            matches = re.findall(pattern, text)

        for plain_url in matches:
            # Try to extract search query and build affiliate link with search
            search_query = ""
            if "search" in plain_url or "querystr" in plain_url or "q=" in plain_url:
                try:
                    from urllib.parse import urlparse, parse_qs
                    parsed = urlparse(plain_url)
                    params = parse_qs(parsed.query)
                    for key in ("text", "querystr", "q", "query", "SearchText", "keyword", "p"):
                        if key in params:
                            search_query = params[key][0]
                            break
                except Exception:
                    pass
            
            if search_query:
                replacement = prog.get_search_url(search_query)
            else:
                replacement = affiliate_url
            
            text = text.replace(plain_url, replacement)
        
        # Pattern 2: Bare domain mentions — rossko.ru or www.rossko.ru (not already part of a longer URL)
        # Only replace if it's NOT already part of an affiliate URL (which would be much longer)
        try:
            bare_matches = find_bare_domains(text, domain)
        except Exception:
            bare_pattern = rf'(?<![/\w.-])(?:www\.)?{re.escape(domain)}(?![/\w.-])'
            bare_matches = re.findall(bare_pattern, text)

        for bare_domain in bare_matches:
            # Check this isn't already inside an affiliate URL
            idx = text.find(bare_domain)
            if idx > 0:
                # Look back — if there's a tracking domain prefix, skip
                before = text[max(0, idx-50):idx]
                # v5.0 EXPANDED — covers ALL admitad tracking domains used by the 25 partners
                tracking_domains = [
                    "ad.admitad.com", ".com/g/",
                    # Specific tracking domains from partners.json
                    "xmknb.com", "twnfz.com", "zmgig.com", "uuwgc.com",
                    "fxxag.com", "ficca2021.com", "bywiola.com", "rzekl.com",
                    "naiawork.com", "ali.click", "yyczo.com", "ujhjj.com",
                    "rcpsj.com", "kdbov.com", "sgkaa.com", "uhtkc.com",
                    "gndrz.com", "hvjjg.com", "dhwnh.com", "thevospad.com",
                ]
                if any(td in before for td in tracking_domains):
                    continue
            text = text.replace(bare_domain, affiliate_url, 1)
    
    return text


def _clean_markdown(text: str) -> str:
    """Remove markdown formatting that Asya shouldn't use.
    
    Markdown links [text](url) are converted to plain text: text — url.
    Other markdown is stripped. No HTML — plain text for chat responses.
    """
    # Convert markdown links [text](url) → text — url (plain text format)
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'\1 — \2', text)
    
    # Remove bold (after escaping, ** won't be in &lt; form)
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    # Remove italic
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    # Remove code blocks
    text = re.sub(r'```[\s\S]*?```', lambda m: m.group(0).strip('`').strip(), text)
    # Remove inline code
    text = re.sub(r'`(.+?)`', r'\1', text)
    # Remove headers
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    # Remove bullet points (convert to simple text)
    text = re.sub(r'^[-*]\s+', '— ', text, flags=re.MULTILINE)
    return text


def _split_message(text: str, max_length: int = 4096) -> list:
    """Split a long message into chunks at paragraph boundaries."""
    if len(text) <= max_length:
        return [text]

    chunks = []
    while text:
        if len(text) <= max_length:
            chunks.append(text)
            break

        # Try to split at newline near the limit
        split_pos = text.rfind('\n', 0, max_length)
        if split_pos < max_length // 2:
            # Try splitting at space
            split_pos = text.rfind(' ', 0, max_length)
        if split_pos < max_length // 2:
            # Hard split
            split_pos = max_length

        chunks.append(text[:split_pos].strip())
        text = text[split_pos:].strip()

    return chunks
