"""
Chat Handler — Main user interaction with AI, web search, partner links,
car diagnostics, spare part search, and personalized communication.
"""

import re
import logging
from typing import Optional

from aiogram import Router, F, types
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery
from aiogram.enums import ChatAction
from aiogram.client.default import DefaultBotProperties

from bot.config import config, persona
from bot.database import (
    get_or_create_user, is_user_blocked, add_chat_message,
    clear_chat_history, get_chat_mode, set_chat_mode,
)
from bot.asya import (
    is_part_number, extract_part_numbers, identify_car_brand,
    detect_symptoms, detect_obd2_codes, lookup_obd2_code,
    build_diagnostic_context, ASYA_PHRASES,
)
from bot.web_search import web_search, search_spare_part, format_search_results
from bot.tech_docs import (
    search_part_by_article, search_diagnostic_code,
    search_repair_procedure, format_part_info, format_tech_context,
)
from bot.partners import partner_manager
from ai.router import ai_router
from ai.voice import process_voice_message

logger = logging.getLogger("asya.handlers.chat")

chat_router = Router()


# ── Gender detection from Russian first name ────────────────────────────────

MALE_NAME_ENDINGS = ("й", "ь", "н", "л", "р", "с", "т", "в", "к", "м", "г", "б", "д", "п", "з", "ж", "х")
FEMALE_NAME_ENDINGS = ("а", "я", "ия", "ья", "ина")

COMMON_MALE_NAMES = {
    "александр", "дмитрий", "максим", "сергей", "андрей", "алексей", "артём",
    "илья", "кирилл", "михаил", "никита", "матвей", "роман", "егор", "арсений",
    "иван", "денис", "евгений", "даниил", "тимур", "владимир", "олег", "павел",
    "руслан", "вадим", "тиmur", "константин", "антон", "ignat", "борис",
}

COMMON_FEMALE_NAMES = {
    "анна", "мария", "ольга", "елена", "наталья", "татьяна", "ирина", "светлана",
    "екатерина", "юлия", "дарья", "алина", "вера", "полина", "кристина", "софия",
    "валерия", "марина", "людмила", "надежда", "людмила", "настя", "анастасия",
    "виктория", "маргарита", "диана", "евгения", "алёна", "катерина",
}


def _guess_gender(first_name: str) -> str:
    """Guess gender from Russian first name. Returns 'male', 'female', or 'unknown'."""
    if not first_name:
        return "unknown"

    name_lower = first_name.lower().strip()

    # Check known name lists
    if name_lower in COMMON_MALE_NAMES:
        return "male"
    if name_lower in COMMON_FEMALE_NAMES:
        return "female"

    # Check ending patterns for Russian names
    if name_lower.endswith(FEMALE_NAME_ENDINGS):
        if name_lower.endswith("ь"):  # Could be male (Игорь) or other
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

    return True


# ── /start command ─────────────────────────────────────────────────────────────

@chat_router.message(CommandStart())
async def cmd_start(message: Message):
    """Handle /start command."""
    if not await _check_user(message):
        return

    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    name = message.from_user.first_name or ""
    gender = _guess_gender(name)

    if name:
        if gender == "male":
            welcome = f"Привет, {name}! Я Ася — живая девушка, которая обожает автомобили 🚗\n\n"
        elif gender == "female":
            welcome = f"Привет, {name}! Я Ася — мы с тобой обе понимаем толк в машинах 🚗💪\n\n"
        else:
            welcome = f"Привет, {name}! Я Ася — живая девушка, которая обожает автомобили 🚗\n\n"
    else:
        welcome = "Привет! Я Ася — живая девушка, которая обожает автомобили 🚗\n\n"

    welcome += (
        "Могу помочь с:\n"
        "🔧 Диагностика поломок — опишите симптомы\n"
        "🔍 Поиск запчастей по артикулу\n"
        "📊 Автомобильные новости и обзоры\n"
        "💡 Любые вопросы про автомобили\n\n"
        "Просто напишите свой вопрос! Я всегда на связи 😊"
    )
    await message.answer(welcome)


# ── /help command ──────────────────────────────────────────────────────────────

@chat_router.message(Command("help"))
async def cmd_help(message: Message):
    """Handle /help command."""
    if not await _check_user(message):
        return

    help_text = (
        "Я Ася — автоэксперт, живая девушка, которая разбирается в машинах 😊\n\n"
        "Вот что я умею:\n\n"
        "🔧 Диагностика — опишите проблему с машиной, помогу разобраться\n"
        "🔍 Запчасти — напишите артикул (OEM-номер), найду где купить\n"
        "📊 Ошибки OBD-II — напишите код ошибки, объясню что значит\n"
        "📰 Новости — расскажу что нового в Автомире\n"
        "💬 Общение — могу обсудить любую тему, но авто — моя страсть!\n\n"
        "Команды:\n"
        "/start — приветствие\n"
        "/help — эта справка\n"
        "/clear — очистить историю чата\n"
        "/diagnostic — режим диагностики\n"
        "/parts — режим поиска запчастей\n"
        "/normal — обычный режим чата"
    )
    await message.answer(help_text)


# ── /clear command ─────────────────────────────────────────────────────────────

@chat_router.message(Command("clear"))
async def cmd_clear(message: Message):
    """Clear chat history."""
    if not await _check_user(message):
        return

    await clear_chat_history(message.from_user.id)
    await message.answer("История чата очищена. Начинаем с чистого листа! 😊")


# ── Mode commands ──────────────────────────────────────────────────────────────

@chat_router.message(Command("diagnostic"))
async def cmd_diagnostic(message: Message):
    """Switch to diagnostic mode."""
    if not await _check_user(message):
        return

    await set_chat_mode(message.from_user.id, "diagnostic")
    await message.answer(
        "Режим диагностики включён 🔧 Опишите проблему с автомобилем — "
        "дам пошаговую диагностику, возможные причины и рекомендации."
    )


@chat_router.message(Command("parts"))
async def cmd_parts(message: Message):
    """Switch to parts search mode."""
    if not await _check_user(message):
        return

    await set_chat_mode(message.from_user.id, "parts")
    await message.answer(
        "Режим поиска запчастей включён 🔍 Напишите артикул (OEM-номер) — "
        "найду информацию о детали и где её купить."
    )


@chat_router.message(Command("normal"))
async def cmd_normal(message: Message):
    """Switch to normal chat mode."""
    if not await _check_user(message):
        return

    await set_chat_mode(message.from_user.id, "normal")
    await message.answer("Обычный режим чата 😊 Спрашивайте что угодно!")


# ── Voice message handler ─────────────────────────────────────────────────────

@chat_router.message(F.voice)
async def handle_voice(message: Message):
    """Handle voice messages — transcribe and process."""
    if not await _check_user(message):
        return

    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    await message.answer("Слушаю... 🎙️")

    voice = message.voice
    text = await process_voice_message(message.bot, voice.file_id)

    if text and not text.startswith("Не удалось"):
        # Process transcribed text as regular message
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
    """Core message processing with AI, search, diagnostics, parts, and personalization."""
    user_id = message.from_user.id
    chat_mode = await get_chat_mode(user_id)

    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    # ── Build extra context ────────────────────────────────────────────────

    extra_context_parts = []

    # 0. User persona context for personalized communication
    user_context = _get_user_persona_context(message)
    if user_context:
        extra_context_parts.append(user_context)

    # 1. Detect car brand
    brand = identify_car_brand(text)
    if brand:
        from bot.asya import get_brand_info
        info = get_brand_info(brand)
        if info:
            extra_context_parts.append(f"Упомянута марка: {brand} ({info['country']}, холдинг: {info['parent']})")

    # 2. Detect OBD-II codes
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

    # 3. Detect part numbers
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

    # 4. Detect car symptoms
    symptoms = detect_symptoms(text)
    is_diagnostic = bool(symptoms) or chat_mode == "diagnostic"

    if symptoms:
        diag_context = build_diagnostic_context(text)
        if diag_context:
            extra_context_parts.append(diag_context)

    # 5. Web search for relevant info
    needs_search = (
        is_diagnostic or
        is_part_query or
        any(kw in text.lower() for kw in [
            "найди", "поиск", "ищи", "где купить", "сколько стоит",
            "новости", "что нового", "обзор", "сравни", "лучший",
            "рекомендуй", "посоветуй", "купить", "заказать",
            "когда", "где", "какой", "какая", "какие",
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

    # 6. Partner program context
    try:
        partner_context = partner_manager.generate_partner_context(text)
        if partner_context:
            extra_context_parts.append(partner_context)
    except Exception as e:
        logger.error(f"Partner context error: {e}")

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

    # ── Send response ──────────────────────────────────────────────────────

    if response.error:
        logger.error(f"AI error: {response.error_message}")
        await message.answer(
            "Ой, что-то я зависла 😅 Дайте мне минутку и попробуйте ещё раз!"
        )
        return

    reply_text = response.text

    # Ensure Asya doesn't use markdown formatting in chat
    reply_text = _clean_markdown(reply_text)

    # Split long messages (Telegram limit 4096 chars)
    if len(reply_text) <= 4096:
        await message.answer(reply_text)
    else:
        # Split at paragraph boundaries
        chunks = _split_message(reply_text, max_length=4096)
        for chunk in chunks:
            await message.answer(chunk)


# ── Utility functions ──────────────────────────────────────────────────────────

def _clean_markdown(text: str) -> str:
    """Remove markdown formatting that Asya shouldn't use."""
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
