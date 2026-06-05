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

    # In groups: Ася should just comment on local model, not use cloud vision
    if is_group:
        # Simple local comment about the photo — no cloud vision
        caption = message.caption or ""
        simple_prompt = (
            f"Кто-то прислал фото в группе. "
            f"{'С подписью: ' + caption[:100] if caption else 'Без подписи.'} "
            f"Напиши короткий комментарий (до 200 символов) как автоэксперт. "
            f"Без анализа фото — просто живой комментарий."
        )
        try:
            response = await ai_router.chat(
                user_id=message.from_user.id,
                message=simple_prompt,
                route_type="comment",  # LOCAL-ONLY
                save_history=False,
                use_cache=False,
            )
            if response.text:
                reply_text = response.text[:COMMENT_MAX_CHARS]
                await message.reply(reply_text)
        except Exception as e:
            logger.debug(f"Group photo comment error: {e}")
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

                # Check if AI found a VIN in the photo — suggest partner links for parts
                detected_vin = _detect_vin(reply_text)
                if detected_vin and len(detected_vin) == 17:
                    try:
                        links = partner_manager.get_primary_parts_links()
                        if links:
                            reply_text += f"\n\nЗапчасти по VIN {detected_vin} можно подобрать здесь:\n"
                            reply_text += "Где купить:\n"
                            link_emojis = ["🔧", "🔍", "🛒"]
                            for i, link in enumerate(links):
                                emoji = link_emojis[i] if i < len(link_emojis) else "🔗"
                                reply_text += f"{emoji} {link['name']} — {link['url']}\n"
                    except Exception as e:
                        logger.debug(f"Primary links from photo error: {e}")

                # Check if AI found part numbers — suggest partner links
                detected_parts = extract_part_numbers(reply_text)
                if detected_parts:
                    try:
                        links = partner_manager.get_primary_parts_links()
                        if links:
                            reply_text += "\n\nГде купить:\n"
                            link_emojis = ["🔧", "🔍", "🛒"]
                            for i, link in enumerate(links):
                                emoji = link_emojis[i] if i < len(link_emojis) else "🔗"
                                reply_text += f"{emoji} {link['name']} — {link['url']}\n"
                    except Exception as e:
                        logger.debug(f"Primary links from photo parts error: {e}")

                # Split if too long
                if len(reply_text) <= config.TELEGRAM_TEXT_LIMIT:
                    await message.answer(reply_text, parse_mode=ParseMode.HTML)
                else:
                    chunks = _split_message(reply_text, max_length=config.TELEGRAM_TEXT_LIMIT)
                    for chunk in chunks:
                        await message.answer(chunk, parse_mode=ParseMode.HTML)
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
    collected_partner_links = []  # List of (name, url) tuples — for clean formatting after AI response

    # 0. User persona context for personalized communication
    user_context = _get_user_persona_context(message)
    if user_context:
        extra_context_parts.append(user_context)

    # 0.1. Inter-bot chat detection — check if message is from Настя (the other bot)
    NASTYA_BOT_USERNAME = "asnastya_bot"
    if (
        message.chat.type in ("group", "supergroup")
        and message.from_user
        and message.from_user.username
        and message.from_user.username.lower() == NASTYA_BOT_USERNAME
    ):
        extra_context_parts.append(
            "Это сообщение от Насти — другого бота, который тоже в этом чате. "
            "Ты можешь отвечать ей и обсуждать темы."
        )
        # Register as shared chat for interbot coordination
        try:
            from bot.interbot import interbot_manager
            interbot_manager.register_shared_chat(message.chat.id, message.chat.title or "")
        except Exception as e:
            logger.debug(f"Interbot register error: {e}")

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
        if vin_code and len(vin_code) == 17:
            try:
                search_query = f"VIN {vin_code} расшифровка автомобиль характеристики"
                results = await web_search(search_query, max_results=3)
                if results:
                    vin_search_context = "Результаты поиска по VIN:\n" + format_search_results(results, max_items=3)
            except Exception as e:
                logger.debug(f"VIN web search error: {e}")
        
        # Add primary partner links (Rossko, Autopiter RU, AvtoALL)
        primary_links_context = ""
        try:
            primary_links_context = partner_manager.format_primary_parts_links()
        except Exception as e:
            logger.debug(f"Primary links context error: {e}")
        
        all_context = extra_context_parts.copy()
        if vin_search_context:
            all_context.append(vin_search_context)
        if primary_links_context:
            all_context.append(primary_links_context)
        
        response = await ai_router.decode_vin(
            user_id=user_id,
            vin_code=vin_or_body,
            extra_context="\n".join(all_context),
        )
        # Collect VIN partner links for clean formatting
        vin_partner_links = []
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
            # Collect primary partner links for post-processing (clean formatting)
            try:
                primary_links_data = partner_manager.get_primary_parts_links()
                for pl in primary_links_data:
                    collected_partner_links.append((pl['name'], pl['url']))
            except Exception:
                pass
        except Exception as e:
            logger.debug(f"Primary links for spare parts error: {e}")

    # 7. Partner program context — additional partner links for non-parts topics
    # (tires, insurance, tools, etc.) — primary parts links already added above
    try:
        # Only add category-based partner context for non-parts topics
        text_lower = text.lower()
        parts_keywords = ["запчаст", "деталь", "артикул", "купить запчас", "vin", "вин"]
        is_only_parts = is_spare_part_query or is_part_query or is_vin_query
        
        if not is_only_parts:
            partner_context = partner_manager.generate_partner_context(text)
            if partner_context:
                extra_context_parts.append(partner_context)
        else:
            # For parts queries, add OTHER partner links (tires, insurance, etc.) if relevant
            other_keywords = ["шины", "диски", "резина", "страховка", "осаго", "каско",
                             "инструмент", "проверк", "аренда"]
            if any(kw in text_lower for kw in other_keywords):
                partner_context = partner_manager.generate_partner_context(text)
                if partner_context:
                    extra_context_parts.append(partner_context)
    except Exception as e:
        logger.error(f"Partner context error: {e}")

    # ── Route to AI ────────────────────────────────────────────────────────

    extra_context = "\n\n".join(extra_context_parts) if extra_context_parts else ""

    # Determine route type based on chat context
    is_group_chat = message.chat.type in ("group", "supergroup")
    is_own_channel = str(message.chat.id) == str(config.CHANNEL_ID)

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
    elif is_group_chat and not is_own_channel:
        # GROUP/SUPERGROUP (not our channel) → LOCAL-ONLY (comment mode)
        # No cloud waste on casual group comments!
        response = await ai_router.chat(
            user_id=user_id,
            message=text,
            extra_context=extra_context,
            route_type="comment",  # LOCAL-ONLY — saves cloud balance!
        )
    else:
        response = await ai_router.chat(
            user_id=user_id,
            message=text,
            extra_context=extra_context,
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

    # Remove raw affiliate URLs that AI may have dumped into the response
    if partner_links:
        reply_text = _clean_raw_partner_urls(reply_text, partner_links)

    # Append cleanly formatted partner links section
    if partner_links:
        partner_section = _format_partner_links_section(partner_links)
        if partner_section:
            reply_text = reply_text.rstrip() + "\n\n" + partner_section

    # ── Enforce character limits based on chat type ──
    # Private chat: max CHAT_MAX_CHARS (1500) — AI asked for 500-1000, this is hard limit
    # Group/supergroup: max GROUP_MAX_CHARS (600) — AI asked for 300, this is hard limit
    # Comment in other group (not our channel): max COMMENT_MAX_CHARS (300)
    if message.chat.type == "private":
        if len(reply_text) > CHAT_MAX_CHARS:
            logger.info(f"Truncating private chat response: {len(reply_text)} → {CHAT_MAX_CHARS} chars")
            reply_text = _smart_truncate(reply_text, CHAT_MAX_CHARS)
    else:
        # Check if this is a comment in another group (not our channel)
        is_own_channel = (
            str(message.chat.id) == str(config.CHANNEL_ID)
            or message.chat.username == config.CHANNEL_ID.replace("@", "")
        )
        if not is_own_channel and message.chat.type in ("group", "supergroup"):
            if len(reply_text) > COMMENT_MAX_CHARS:
                logger.info(f"Truncating comment in other group: {len(reply_text)} → {COMMENT_MAX_CHARS} chars")
                reply_text = _smart_truncate(reply_text, COMMENT_MAX_CHARS)
        elif len(reply_text) > GROUP_MAX_CHARS:
            # Group or supergroup — keep it short!
            logger.info(f"Truncating group response: {len(reply_text)} → {GROUP_MAX_CHARS} chars")
            reply_text = _smart_truncate(reply_text, GROUP_MAX_CHARS)

    # Split long messages (Telegram limit 4096 chars)
    # Use HTML parse mode since partner links use <a> tags
    if len(reply_text) <= config.TELEGRAM_TEXT_LIMIT:
        await message.answer(reply_text, parse_mode=ParseMode.HTML)
    else:
        chunks = _split_message(reply_text, max_length=config.TELEGRAM_TEXT_LIMIT)
        for chunk in chunks:
            await message.answer(chunk, parse_mode=ParseMode.HTML)


# ── Utility functions ──────────────────────────────────────────────────────────

# Shop icon mapping for clean link formatting
_SHOP_ICONS = {
    "rossko": "🔧",
    "autopiter": "🔍",
    "avtoall": "🛒",
    "exist": "📋",
    "emex": "🔩",
    "autodoc": "🚗",
    "zzap": "💰",
    "ixora": "⚙️",
}
_DEFAULT_SHOP_ICON = "🔗"


def _format_partner_links_section(partner_links: list) -> str:
    """Format partner links as a clean, readable section with HTML clickable links.
    
    Takes a list of (name, url) tuples and returns a nicely formatted
    string with HTML links so the user sees only the shop name, not the
    long tracking URL.
    
    Result looks like:
    
    Где купить:
    🔧 <a href="https://...tracking...">Росско</a>
    🔍 <a href="https://...tracking...">Autopiter</a>
    🛒 <a href="https://...tracking...">AvtoALL</a>
    """
    if not partner_links:
        return ""
    
    lines = ["Где купить:"]
    for name, url in partner_links[:5]:
        icon = _DEFAULT_SHOP_ICON
        name_lower = name.lower()
        for shop_key, shop_icon in _SHOP_ICONS.items():
            if shop_key in name_lower:
                icon = shop_icon
                break
        # Use HTML <a> tag so user sees only the name, URL is hidden
        lines.append(f'{icon} <a href="{url}">{name}</a>')
    
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


def _clean_markdown(text: str) -> str:
    """Remove markdown formatting that Asya shouldn't use, but convert links to HTML.
    
    Markdown links [text](url) are converted to HTML <a href="url">text</a>
    for Telegram's HTML mode. Other markdown is stripped.
    Also escapes HTML special chars to avoid breaking Telegram's HTML parser.
    """
    import html as html_module
    
    # First, convert markdown links [text](url) → HTML <a href="url">text</a>
    # Do this BEFORE escaping so the link syntax isn't mangled
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', text)
    
    # Now escape HTML special chars in the NON-link parts
    # We need to preserve our <a> tags while escaping everything else
    # Split by <a> tags, escape the parts between them, then rejoin
    parts = re.split(r'(<a\s+href="[^"]*">[^<]*</a>)', text)
    for i, part in enumerate(parts):
        if not re.match(r'<a\s+href="[^"]*">[^<]*</a>', part):
            # This is a text part — escape HTML special chars
            parts[i] = html_module.escape(part, quote=False)
    text = ''.join(parts)
    
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
