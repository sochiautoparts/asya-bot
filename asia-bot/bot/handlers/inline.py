"""Asya Inline Mode Handler v1.0 — Ася в любом чате!

Когда пользователь пишет @asiaexp_bot запрос в любом чате,
Ася отвечает коротким сообщением прямо в инлайн-режиме.

Особенности:
  - Короткие ответы (макс 200 символов) для инлайн-режима
  - AI-генерация через Pollinations API
  - Кэширование результатов на 5 минут
  - Fallback на шаблонные ответы если AI недоступен
  - Кнопка "Подробнее" с диплинком в приватный чат
"""
import asyncio
import hashlib
import logging
import time
from aiogram import Router, F
from aiogram.types import (
    InlineQuery, InlineQueryResultArticle,
    InputTextMessageContent, InlineKeyboardMarkup, InlineKeyboardButton,
)
from bot.config import BOT_USERNAME, INLINE_CACHE_TIME

logger = logging.getLogger(__name__)
router = Router()

# Cache for inline responses
_inline_cache: dict = {}  # query_hash -> {"text": str, "time": float}
_INLINE_CACHE_TTL = 300  # 5 minutes

# Quick template responses for when AI is unavailable — auto expert style
QUICK_RESPONSES = [
    "Ася на связи! 🔧",
    "Ася тут! Автоэксперт к вашим услугам! 🚗",
    "Привет от Аси! Сейчас разберёмся! 🔧",
    "Ася слышит! 🚙",
    "Автоэксперт Ася на линии! ⚙️",
    "Ну что, спрашивай! Ася готова! 🔧",
    "Ася на связи! Давай вопрос! 🚗",
    "О, Асю вызывают! ⚙️",
    "Привет! Ася уже здесь! 🔧",
    "Слушаю! Автоэксперт Ася! 🚙",
    "Ася в деле! Давай! 🔧",
    "Ну наконец-то! Ася! 🚗",
    "Ой, меня позвали! Бегу! 🏃‍♀️🔧",
    "Ася готова! Какая проблема? ⚙️",
    "Автоэксперт на проводе! 🔧",
    "Ау! Ася слышит! 👂🚗",
    "Ну? Ася ждёт! ⚙️",
    "Привеееет! Ася! 🔧",
    "Ася на проводе! 📞🚙",
    "О! Ася пришла! 🎉🔧",
]


def _get_cache_key(query: str, user_id: int) -> str:
    """Generate cache key from query and user."""
    raw = f"{query.lower().strip()}:{user_id}"
    return hashlib.md5(raw.encode()).hexdigest()


def _cleanup_cache() -> None:
    """Remove expired cache entries."""
    now = time.time()
    expired = [k for k, v in _inline_cache.items() if now - v["time"] > _INLINE_CACHE_TTL]
    for k in expired:
        del _inline_cache[k]


@router.inline_query(F.query != "")
async def handle_inline_query(inline_query: InlineQuery, db=None, ai_router=None) -> None:
    """Handle inline query with AI response or template fallback.

    When user types @asiaexp_bot query in any chat, Asya responds
    with a short, expert answer.
    """
    query = inline_query.query.strip()
    user_id = inline_query.from_user.id

    if not query:
        return

    # Check cache first
    _cleanup_cache()
    cache_key = _get_cache_key(query, user_id)
    if cache_key in _inline_cache:
        cached = _inline_cache[cache_key]
        result_text = cached["text"]
    else:
        result_text = None

    # Try AI generation if not cached
    if result_text is None:
        result_text = await _generate_inline_response(query, user_id, ai_router)
        # Cache the result
        _inline_cache[cache_key] = {"text": result_text, "time": time.time()}

    # Build inline result
    # Deep link to bot private chat for "Подробнее"
    detail_url = f"https://t.me/{BOT_USERNAME}?start=chat"

    results = [
        InlineQueryResultArticle(
            id=f"asya_{hashlib.md5(query.encode()).hexdigest()[:12]}",
            title=f"Ася: {query[:50]}",
            description=result_text[:80] if len(result_text) > 80 else result_text,
            input_message_content=InputTextMessageContent(
                message_text=result_text,
            ),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="🔧 Подробнее у Аси",
                    url=detail_url,
                )],
            ]),
        )
    ]

    await inline_query.answer(
        results=results,
        cache_time=INLINE_CACHE_TIME,
        is_personal=True,
    )


@router.inline_query()
async def handle_empty_inline_query(inline_query: InlineQuery, db=None, ai_router=None) -> None:
    """Handle empty inline query with suggestions.

    When user just types @asiaexp_bot without a query,
    show some helpful suggestions.
    """
    suggestions = [
        ("Ася, диагностика! 🔧", "Помощь с диагностикой авто"),
        ("Найди запчасть, Ася! ⚙️", "Поиск автозапчастей"),
        ("Ася, что значит P0420? 🚗", "Расшифровка OBD2 кодов"),
        ("Ася, какие новости? 📰", "Автомобильные новости"),
    ]

    results = []
    for i, (text, desc) in enumerate(suggestions):
        results.append(
            InlineQueryResultArticle(
                id=f"asya_suggest_{i}",
                title=text,
                description=desc,
                input_message_content=InputTextMessageContent(
                    message_text=text,
                ),
            )
        )

    await inline_query.answer(
        results=results,
        cache_time=60,
        is_personal=False,
    )


async def _generate_inline_response(query: str, user_id: int, ai_router) -> str:
    """Generate a short Asya-style response for inline mode.

    Uses Pollinations API for AI generation.
    Falls back to template responses on failure.
    Max 200 characters for inline mode.
    """
    # Try AI generation
    if ai_router and ai_router._pollinations and ai_router._pollinations.is_available():
        try:
            from bot.config import ASYA_SYSTEM_PROMPT

            inline_system = (
                "Ты Ася — автоэксперт, ведёшь канал @sochiautoparts. Отвечай ОЧЕНЬ КОРОТКО, "
                "максимум 1-2 предложения, до 200 символов. "
                "Профессионально но дружелюбно, как хороший механик. "
                "Без markdown, без буллетов. "
                "Используй слова: 'смотрите', 'по сути', 'кстати', 'по моему опыту'. "
                "Если вопрос сложный — ответь коротко и предложи обсудить подробнее."
            )

            result = await asyncio.wait_for(
                ai_router._pollinations.generate(
                    prompt=query,
                    system_prompt=inline_system,
                    max_tokens=80,
                    temperature=0.9,
                ),
                timeout=8.0,
            )

            if result and result.text:
                # Clean and truncate
                from ai.router import AIRouter
                text = AIRouter.clean_ai_response(result.text)
                if text:
                    # Truncate to 200 chars at sentence boundary
                    if len(text) > 200:
                        for sep in ['. ', '! ', '? ', '\n', ', ']:
                            idx = text[:200].rfind(sep)
                            if idx > 50:
                                text = text[:idx + len(sep)].strip()
                                break
                        else:
                            text = text[:197] + "..."
                    return text

        except asyncio.TimeoutError:
            logger.warning(f"Inline AI timeout for query: {query[:50]}")
        except Exception as e:
            logger.warning(f"Inline AI error: {e}")

    # Fallback: template response
    import random
    return random.choice(QUICK_RESPONSES)
