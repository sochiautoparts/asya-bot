"""
Chat Handler — Main user interaction with AI, web search, partner links,
car diagnostics, spare part search, VIN decoding, photo analysis,
and personalized communication with conversation context.
"""

import re
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

logger = logging.getLogger("asya.handlers.chat")

chat_router = Router()


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

    import random
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
        "🔍 Найти запчасть — кинь артикул, я поищу\n"
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
        "Ищем запчасти 🔍 Кидай артикул — и я поищу"
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
    """Handle photo messages — analyze with vision AI."""
    if not await _check_user(message):
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
            response = await client.get(file_url)
            if response.status_code == 200:
                image_base64 = base64.b64encode(response.content).decode("utf-8")

                # Determine media type
                media_type = "image/jpeg"
                if file_info.file_path.endswith(".png"):
                    media_type = "image/png"
                elif file_info.file_path.endswith(".webp"):
                    media_type = "image/webp"

                extra_context = "\n\n".join(extra_context_parts) if extra_context_parts else ""

                response = await ai_router.analyze_image(
                    user_id=message.from_user.id,
                    image_base64=image_base64,
                    prompt=prompt,
                    extra_context=extra_context,
                )

                if response.error or not response.text:
                    await message.answer("Ой, не получилось разглядеть фото 😅 Попробуй ещё раз!")
                    return

                reply_text = response.text
                reply_text = _clean_markdown(reply_text)

                # Check if AI found a VIN in the photo — if so, also search parts by VIN
                detected_vin = _detect_vin(reply_text)
                if detected_vin and len(detected_vin) == 17:
                    try:
                        vin_parts = await search_parts_by_vin(detected_vin, max_results=5)
                        if vin_parts:
                            vin_links = "\n\nЗапчасти по этому VIN:\n"
                            for r in vin_parts[:5]:
                                vin_links += f"— {r.title}: {r.url}\n"
                            reply_text += vin_links
                    except Exception as e:
                        logger.debug(f"VIN parts search from photo error: {e}")

                # Check if AI found part numbers — add direct purchase links
                detected_parts = extract_part_numbers(reply_text)
                if detected_parts:
                    for article in detected_parts[:2]:
                        try:
                            part_results = await search_spare_part(article, max_results=3)
                            if part_results:
                                part_links = f"\n\n{article} — где купить:\n"
                                for r in part_results[:3]:
                                    part_links += f"— {r.url}\n"
                                reply_text += part_links
                        except Exception:
                            pass

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

    await _process_text_message(message, text)


async def _process_text_message(message: Message, text: str):
    """Core message processing with AI, search, diagnostics, parts, VIN, and personalization."""
    import random
    user_id = message.from_user.id
    chat_mode = await get_chat_mode(user_id)

    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)

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

    # 0. User persona context for personalized communication
    user_context = _get_user_persona_context(message)
    if user_context:
        extra_context_parts.append(user_context)

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
        
        # Try web search for VIN info (car history, specs, etc.)
        vin_search_context = ""
        vin_parts_context = ""
        if vin_code and len(vin_code) == 17:
            try:
                search_query = f"VIN {vin_code} расшифровка автомобиль характеристики"
                results = await web_search(search_query, max_results=3)
                if results:
                    vin_search_context = "Результаты поиска по VIN:\n" + format_search_results(results, max_items=3)
            except Exception as e:
                logger.debug(f"VIN web search error: {e}")
            
            # Also search for parts by VIN — give user direct shop links
            try:
                # Check if user also mentions a specific part
                part_name = ""
                part_keywords = ["колодки", "фильтр", "свечи", "ремень", "амортизатор", "подшипник",
                                 "сальник", "прокладк", "датчик", "реле", "насос", "стойка",
                                 "шаровая", "наконечник", "тяга", "сцепление", "диск", "барабан",
                                 "катушк", "генератор", "стартер", "компрессор", "радиатор",
                                 "термостат", "помп", "глушитель", "подушка", "опора"]
                for kw in part_keywords:
                    if kw in text.lower():
                        part_name = kw
                        break
                
                vin_parts = await search_parts_by_vin(vin_code, part_name=part_name, max_results=5)
                if vin_parts:
                    vin_parts_context = "Ссылки на подбор запчастей по VIN (вставь естественно в ответ):\n"
                    for r in vin_parts[:5]:
                        vin_parts_context += f"— {r.title}: {r.url}\n"
            except Exception as e:
                logger.debug(f"VIN parts search error: {e}")
        
        all_context = extra_context_parts.copy()
        if vin_search_context:
            all_context.append(vin_search_context)
        if vin_parts_context:
            all_context.append(vin_parts_context)
        
        response = await ai_router.decode_vin(
            user_id=user_id,
            vin_code=vin_or_body,
            extra_context="\n".join(all_context),
        )
        await _send_response(message, response, status_msg)
        return

    # 2. Detect car brand
    brand = identify_car_brand(text)
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

        # Search for detailed info on the code
        for code in obd_codes[:2]:
            try:
                code_info = await search_diagnostic_code(code)
                if code_info.get("links"):
                    links_text = "\n".join(
                        f"- {l['title']}: {l['url']}" for l in code_info["links"][:3]
                    )
                    extra_context_parts.append(f"Подробности по ошибке {code}:\n{links_text}")
            except Exception as e:
                logger.error(f"Error searching diagnostic code: {e}")

    # 4. Detect part numbers
    part_numbers = extract_part_numbers(text)
    is_part_query = bool(part_numbers) or is_part_number(text.strip()) or chat_mode == "parts"

    if is_part_query:
        articles = part_numbers or [text.strip()]
        for article in articles[:3]:
            try:
                part_info = await search_part_by_article(article)
                extra_context_parts.append(format_part_info(part_info))
            except Exception as e:
                logger.error(f"Error searching part: {e}")

    # 5. Detect car symptoms
    symptoms = detect_symptoms(text)
    is_diagnostic = bool(symptoms) or chat_mode == "diagnostic"

    if symptoms:
        diag_context = build_diagnostic_context(text)
        if diag_context:
            extra_context_parts.append(diag_context)

    # 6. Web search for relevant info — expanded triggers for better search coverage
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

    if needs_search:
        try:
            search_query = text
            if brand:
                search_query = f"{brand} {text}"
            results = await web_search(search_query, max_results=3)
            if results:
                extra_context_parts.append("Результаты поиска:\n" + format_search_results(results, max_items=3))
        except Exception as e:
            logger.error(f"Web search error: {e}")

    # 6.5. Spare part search — if user mentions parts/articles, search shops specifically
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

    if is_spare_part_query and part_numbers:
        try:
            for article in part_numbers[:3]:
                part_results = await search_spare_part(article, max_results=5)
                if part_results:
                    extra_context_parts.append(
                        f"Результаты поиска запчастей по артикулу {article}:\n"
                        + format_search_results(part_results, max_items=5)
                    )
        except Exception as e:
            logger.error(f"Spare part search error: {e}")
    elif is_spare_part_query and brand:
        # No article but mentions brand + parts — general search
        try:
            part_query = f"{brand} запчасти купить"
            if is_diagnostic and symptoms:
                part_query = f"{brand} {' '.join(symptoms[:2])} запчасти замена"
            results = await search_spare_part(part_query, max_results=5)
            if results:
                extra_context_parts.append(
                    "Результаты поиска запчастей:\n"
                    + format_search_results(results, max_items=5)
                )
        except Exception as e:
            logger.error(f"Brand spare part search error: {e}")

    # 7. Partner program context (including Rossko professional parts selection)
    try:
        partner_context = partner_manager.generate_partner_context(text)
        if partner_context:
            extra_context_parts.append(partner_context)
    except Exception as e:
        logger.error(f"Partner context error: {e}")

    # 7.5. Additional partner links for parts queries — give AI direct shop links
    if is_spare_part_query or is_part_query:
        try:
            search_article = part_numbers[0] if part_numbers else text.strip()
            partner_links = partner_manager.get_all_partner_links_for_parts(search_article)
            if partner_links:
                link_lines = ["Ссылки на магазины запчастей (вставь естественно в ответ, ОБЯЗАТЕЛЬНО с описанием!):"]
                for pl in partner_links:
                    if pl['name'] == "Росско":
                        link_lines.append(f"- Росско (профессиональный подбор, советую начать тут): {pl['url']}")
                    else:
                        link_lines.append(f"- {pl['name']}: {pl['url']}")
                    if pl['description']:
                        link_lines.append(f"  {pl['description']}")
                extra_context_parts.append("\n".join(link_lines))
            
            # Also add direct shop links from search module
            from bot.web_search import SHOP_SEARCH_URLS
            from urllib.parse import quote_plus
            direct_links = []
            article_clean = search_article.strip().upper()
            for shop_name, url_template in SHOP_SEARCH_URLS.items():
                shop_url = url_template.format(article=quote_plus(article_clean))
                direct_links.append(f"- {shop_name.capitalize()}: {shop_url}")
            if direct_links:
                extra_context_parts.append(
                    f"Прямые ссылки на поиск {article_clean} в магазинах:\n" + "\n".join(direct_links)
                )
        except Exception as e:
            logger.error(f"Partner links error: {e}")

    # ── Route to AI ────────────────────────────────────────────────────────

    extra_context = "\n\n".join(extra_context_parts) if extra_context_parts else ""

    if is_diagnostic:
        response = await ai_router.diagnose_car(
            user_id=user_id,
            symptoms=text,
            extra_context=extra_context,
        )
    elif is_part_query:
        response = await ai_router.find_spare_part(
            user_id=user_id,
            article=part_numbers[0] if part_numbers else text.strip(),
            part_info=extra_context,
        )
    else:
        response = await ai_router.chat(
            user_id=user_id,
            message=text,
            extra_context=extra_context,
        )

    await _send_response(message, response, status_msg)


async def _send_response(message: Message, response, status_msg=None):
    """Send AI response to user, handling errors and length limits."""
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

    # Split long messages (Telegram limit 4096 chars)
    if len(reply_text) <= config.TELEGRAM_TEXT_LIMIT:
        await message.answer(reply_text)
    else:
        chunks = _split_message(reply_text, max_length=config.TELEGRAM_TEXT_LIMIT)
        for chunk in chunks:
            await message.answer(chunk)


# ── Utility functions ──────────────────────────────────────────────────────────

def _clean_markdown(text: str) -> str:
    """Remove markdown formatting that Asya shouldn't use, but preserve URLs."""
    # Convert markdown links [text](url) → url (keep the URL, remove the text wrapper)
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'\2', text)
    # Remove bold
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
