"""
Inline Handler — @asiaexp_bot inline mode.
Users can type @asiaexp_bot <query> in any chat to get AI-powered auto expert responses.
Supports: general questions, VIN decoding, car diagnostics, spare part search.
"""

import logging
import hashlib
from typing import Optional, List

from aiogram import Router, F, types
from aiogram.types import (
    InlineQuery,
    InlineQueryResultArticle,
    InlineQueryResultPhoto,
    InputTextMessageContent,
    ChosenInlineResult,
)

from bot.config import config, persona
from bot.asya import (
    is_part_number, identify_car_brand, detect_symptoms,
    detect_obd2_codes, lookup_obd2_code,
)
from bot.partners import partner_manager
from ai.router import ai_router

logger = logging.getLogger("asya.handlers.inline")

inline_router = Router()

# ── VIN detection for inline ─────────────────────────────────────────────────

import re

_VIN_PATTERN = re.compile(r'\b[A-HJ-NPR-Z0-9]{17}\b', re.IGNORECASE)


def _detect_vin_inline(text: str) -> Optional[str]:
    """Detect a VIN code in inline query text."""
    match = _VIN_PATTERN.search(text.upper())
    if match:
        vin = match.group(0)
        if len(vin) == 17 and vin[8] in '0123456789X':
            return vin
    return None


def _detect_query_type(text: str) -> str:
    """Detect what type of query this is for inline mode."""
    text_lower = text.lower().strip()

    # VIN decoding
    if _detect_vin_inline(text) or any(kw in text_lower for kw in ["vin", "вин", "vin-код", "вин код"]):
        return "vin"

    # OBD-II codes
    if detect_obd2_codes(text):
        return "obd2"

    # Spare parts
    if is_part_number(text.strip()) or any(kw in text_lower for kw in [
        "запчасть", "артикул", "купить деталь", "номер детали", "oem",
    ]):
        return "parts"

    # Diagnostics
    if detect_symptoms(text) or any(kw in text_lower for kw in [
        "не заводится", "стук", "вибрация", "перегрев", "чек", "check engine",
        "диагностика", "поломка", "проблема с", "горит",
    ]):
        return "diagnostic"

    # Default: general chat
    return "general"


# ── Inline query handler ──────────────────────────────────────────────────────

@inline_router.inline_query()
async def handle_inline_query(inline_query: InlineQuery):
    """Handle inline queries — @asiaexp_bot <query>."""
    query = inline_query.query.strip()

    if not query:
        # Show help when no query is typed
        results = [
            InlineQueryResultArticle(
                id="help",
                title="Ася — Автоэксперт",
                description="Введите вопрос, VIN-код, артикул запчасти или опишите проблему",
                input_message_content=InputTextMessageContent(
                    message_text=(
                        "🚗 Ася — ваш автоэксперт!\n\n"
                        "Я могу помочь с:\n"
                        "• Расшифровкой VIN — отправьте VIN-код\n"
                        "• Диагностикой — опишите проблему\n"
                        "• Поиском запчастей — укажите артикул\n"
                        "• Вопросами об автомобилях — просто спросите\n\n"
                        "Напишите @asiaexp_bot и ваш вопрос!"
                    ),
                ),
                thumbnail_url="https://cdn-icons-png.flaticon.com/128/3097/3097180.png",
            ),
        ]
        await inline_query.answer(results, cache_time=30)
        return

    # Determine query type
    query_type = _detect_query_type(query)

    # Generate AI response based on query type
    # Inline must respond within ~20 seconds (Telegram limit)
    try:
        import asyncio
        # Pass the actual user_id from inline_query to avoid cache conflicts
        response = await asyncio.wait_for(
            _generate_inline_response(query, query_type, user_id=inline_query.from_user.id),
            timeout=20.0
        )

        if response.error or not response.text:
            results = [
                InlineQueryResultArticle(
                    id="error",
                    title="Не удалось получить ответ",
                    description="Попробуйте переформулировать вопрос",
                    input_message_content=InputTextMessageContent(
                        message_text=f"🚗 Вопрос: {query}\n\n⚠️ Не удалось получить ответ от Аси. Попробуйте написать в личку @asiaexp_bot",
                    ),
                ),
            ]
            await inline_query.answer(results, cache_time=10)
            return

        # Clean markdown from response
        reply_text = _clean_markdown(response.text)

        # Truncate for inline (Telegram limit for InputTextMessageContent is 4096)
        if len(reply_text) > 4000:
            reply_text = reply_text[:3997] + "..."

        # Build results based on query type
        results = _build_inline_results(query, reply_text, query_type)

        await inline_query.answer(results, cache_time=60, is_personal=True)

    except asyncio.TimeoutError:
        logger.warning(f"Inline query timed out: {query[:50]}")
        results = [
            InlineQueryResultArticle(
                id="timeout",
                title="Ася думает слишком долго",
                description="Напишите в личку @asiaexp_bot для быстрого ответа",
                input_message_content=InputTextMessageContent(
                    message_text=f"🚗 Вопрос: {query}\n\n⏱️ Ася не успела ответить. Напишите в личку @asiaexp_bot",
                ),
            ),
        ]
        await inline_query.answer(results, cache_time=10)
    except Exception as e:
        logger.error(f"Inline query error: {e}")
        results = [
            InlineQueryResultArticle(
                id="error",
                title="Ошибка обработки",
                description="Попробуйте ещё раз",
                input_message_content=InputTextMessageContent(
                    message_text=f"🚗 Вопрос: {query}\n\n⚠️ Произошла ошибка. Напишите в личку @asiaexp_bot",
                ),
            ),
        ]
        await inline_query.answer(results, cache_time=10)


async def _generate_inline_response(query: str, query_type: str, user_id: int = 0):
    """Generate AI response for inline query.
    
    Uses a unique negative user_id for inline queries to avoid cache conflicts
    with real users. This prevents inline queries from polluting real user
    chat history and vice versa.
    """
    # Use negative user_id to avoid conflicts with real users
    # user_id 0 could collide with actual user_id 0 (unlikely but possible)
    inline_user_id = -(hash(query) % 100000) - 1  # Always negative, unique per query

    if query_type == "vin":
        vin_code = _detect_vin_inline(query) or query.strip()
        return await ai_router.decode_vin(
            user_id=inline_user_id,
            vin_code=vin_code,
        )

    elif query_type == "diagnostic":
        return await ai_router.diagnose_car(
            user_id=inline_user_id,
            symptoms=query,
        )

    elif query_type == "parts":
        response = await ai_router.find_spare_part(
            user_id=inline_user_id,
            article=query.strip(),
        )
        # Add partner links to inline parts response
        if response and response.text:
            try:
                primary_links = partner_manager.get_primary_parts_links()
                if primary_links:
                    links_section = "\n\nГде купить:"
                    for link in primary_links[:3]:
                        links_section += f"\n{link['name']} — {link['url']}"
                    response.text += links_section
            except Exception:
                pass
        return response

    else:
        # General chat
        return await ai_router.chat(
            user_id=inline_user_id,
            message=query,
            use_cache=True,
            save_history=False,
        )


def _build_inline_results(query: str, reply_text: str, query_type: str) -> List:
    """Build inline query results with contextual titles and descriptions."""
    results = []

    # Main result — the AI answer
    title_map = {
        "vin": "🔓 Расшифровка VIN",
        "diagnostic": "🔧 Диагностика",
        "parts": "🔍 Запчасть",
        "obd2": "📊 Код ошибки",
        "general": "🚗 Ответ Аси",
    }

    desc_map = {
        "vin": f"Полная расшифровка: {query[:30]}",
        "diagnostic": "Возможные причины и рекомендации",
        "parts": f"Информация по: {query[:30]}",
        "obd2": "Описание ошибки и решения",
        "general": f"Ответ на: {query[:40]}",
    }

    main_id = hashlib.md5(f"main_{query}".encode()).hexdigest()[:16]

    results.append(
        InlineQueryResultArticle(
            id=main_id,
            title=title_map.get(query_type, "🚗 Ответ Аси"),
            description=desc_map.get(query_type, ""),
            input_message_content=InputTextMessageContent(
                message_text=reply_text,
            ),
            thumbnail_url="https://cdn-icons-png.flaticon.com/128/3097/3097180.png",
        )
    )

    # Short version — compact answer (first 200 chars)
    short_text = reply_text
    if len(short_text) > 200:
        # Find a good break point
        break_pos = short_text.rfind('\n', 0, 200)
        if break_pos < 100:
            break_pos = short_text.rfind('. ', 0, 200)
        if break_pos < 100:
            break_pos = 197
        short_text = short_text[:break_pos].rstrip() + "..."

    short_id = hashlib.md5(f"short_{query}".encode()).hexdigest()[:16]

    results.append(
        InlineQueryResultArticle(
            id=short_id,
            title="📝 Краткий ответ",
            description=short_text[:80],
            input_message_content=InputTextMessageContent(
                message_text=short_text,
            ),
            thumbnail_url="https://cdn-icons-png.flaticon.com/128/4195/4195liberty.png",
        )
    )

    # For VIN — add "detailed" option
    if query_type == "vin":
        detail_id = hashlib.md5(f"detail_{query}".encode()).hexdigest()[:16]
        results.append(
            InlineQueryResultArticle(
                id=detail_id,
                title="📋 Подробная расшифровка",
                description="Марка, модель, год, двигатель, комплектация",
                input_message_content=InputTextMessageContent(
                    message_text=reply_text,
                ),
            )
        )

    return results


@inline_router.chosen_inline_result()
async def handle_chosen_inline_result(chosen: ChosenInlineResult):
    """Log when a user selects an inline result."""
    logger.info(
        f"Inline result chosen: query='{chosen.query}', "
        f"result_id='{chosen.result_id}', "
        f"user={chosen.from_user.id}"
    )


# ── Utility ────────────────────────────────────────────────────────────────────

def _clean_markdown(text: str) -> str:
    """Remove markdown formatting for inline responses."""
    import re
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'\1', text)
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    text = re.sub(r'```[\s\S]*?```', lambda m: m.group(0).strip('`').strip(), text)
    text = re.sub(r'`(.+?)`', r'\1', text)
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^[-*]\s+', '— ', text, flags=re.MULTILINE)
    return text
