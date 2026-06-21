"""
Guest Mode Handler v1.0
Bot API 10.0 (May 2026) — guest mode lets bots receive messages and reply in
chats they are NOT a member of.

aiogram 3.15 (released before Bot API 10.0) doesn't natively support:
  - Update.guest_message field
  - User.supports_guest_queries field
  - Message.guest_query_id / guest_bot_caller_user / guest_bot_caller_chat
  - answerGuestQuery method
  - SentGuestMessage class

BUT aiogram 3.x uses Pydantic v2 with model_config['extra'] = 'allow' for the
Update model, so unknown fields ARE preserved in `update.model_extra`. This
lets us intercept guest_message updates via a Dispatcher outer middleware.

Strategy:
  1. Register an outer middleware on dp.update that inspects each Update
  2. If `guest_message` is in update.model_extra, extract it
  3. Detect topic (tyres? oil? VIN? parts?) and build a response
  4. Call answerGuestQuery via raw HTTP POST to api.telegram.org
     (we can't use ai_router.something() because aiogram doesn't know this method)

Two response modes:
  - TEXT: short AI-generated answer in Ася's voice (max 600 chars)
  - SHOP_CARDS: if the topic matches a shop category, attach up to 4 product
    cards as inline buttons (uses the same _build_topic_shop_keyboard logic
    as comment_on_group_post, but routed through guest mode)

Reference: https://core.telegram.org/bots/api#may-8-2026
"""

import asyncio
import logging
import re
from typing import Any, Dict, Optional

import httpx

from aiogram import BaseMiddleware, Bot
from aiogram.types import InlineKeyboardMarkup, Update

from bot.config import config
from bot.shop import category_for_text
from bot.database import get_unposted_shop_products

logger = logging.getLogger("asya.guest_mode")

# ── Constants ─────────────────────────────────────────────────────────────────

GUEST_RESPONSE_MAX_CHARS = 800  # Telegram limit for answerGuestQuery is generous, keep it readable
TELEGRAM_API_BASE = "https://api.telegram.org"

# Track recently answered guest_query_ids to prevent double-answering
# (Telegram can deliver the same update twice on retry)
_recent_guest_query_ids: Dict[str, float] = {}
_MAX_RECENT_IDS = 200


def _is_already_answered(guest_query_id: str) -> bool:
    """Check if we already answered this guest_query_id. Returns True if dup."""
    if not guest_query_id:
        return False
    now = asyncio.get_event_loop().time()
    # Cleanup old entries (>5 min)
    stale = [k for k, v in _recent_guest_query_ids.items() if now - v > 300]
    for k in stale:
        _recent_guest_query_ids.pop(k, None)
    if guest_query_id in _recent_guest_query_ids:
        return True
    _recent_guest_query_ids[guest_query_id] = now
    # Cap size
    if len(_recent_guest_query_ids) > _MAX_RECENT_IDS:
        oldest = sorted(_recent_guest_query_ids.items(), key=lambda x: x[1])[:_MAX_RECENT_IDS // 2]
        for k, _ in oldest:
            _recent_guest_query_ids.pop(k, None)
    return False


# ── Raw Bot API caller ────────────────────────────────────────────────────────

async def call_bot_api_raw(bot_token: str, method: str, payload: Dict[str, Any],
                           timeout: float = 15.0) -> Dict[str, Any]:
    """Call a Telegram Bot API method via raw HTTP POST.

    Used for Bot API 10.0+ methods that aiogram 3.15 doesn't support
    (answerGuestQuery, sendRichMessage, etc.).

    Args:
        bot_token: Bot API token
        method: Telegram Bot API method name (e.g. 'answerGuestQuery')
        payload: dict that will be JSON-encoded as the request body
        timeout: HTTP timeout

    Returns:
        Parsed response dict from Telegram (with ok, result, etc.)
    """
    url = f"{TELEGRAM_API_BASE}/bot{bot_token}/{method}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            data = resp.json()
            if not data.get("ok"):
                logger.warning(
                    f"Telegram API {method} returned error: "
                    f"{data.get('error_code')} {data.get('description', '')[:200]}"
                )
            return data
    except Exception as e:
        logger.error(f"call_bot_api_raw({method}) failed: {e}")
        return {"ok": False, "description": str(e)}


# ── Guest message response builder ────────────────────────────────────────────

async def _generate_guest_response(text: str) -> str:
    """Generate a short Ася-style response to a guest message using the LOCAL MODEL.

    Falls back to a static template if the AI is unavailable.
    """
    from ai.router import ai_router

    # Local-first for guest replies (same rule as group comments)
    local_provider = getattr(ai_router, "_local", None)
    if local_provider and await local_provider.is_available():
        try:
            messages = [
                {
                    "role": "system",
                    "content": (
                        "Ты Ася — автоэксперт канала @sochiautoparts. "
                        "Тебе задали вопрос в гостевом режиме (бот не состоит в чате, "
                        "но пользователь хочет ответ от тебя). "
                        "Дай КРАТКИЙ, полезный ответ до 600 символов. "
                        "Без markdown, без буллетов. Живо, как живой человек. "
                        "Если вопрос не про автомобили — коротко откажись и предложи авто-тему."
                    ),
                },
                {"role": "user", "content": text[:1000]},
            ]
            response = await local_provider.chat(
                messages=messages,
                temperature=0.7,
                max_tokens=400,
            )
            if response and not response.error and response.text:
                reply = response.text.strip()
                reply = re.sub(r"<[^>]+>", "", reply)
                reply = re.sub(r"\*\*.*?\*\*", "", reply)
                if len(reply) > GUEST_RESPONSE_MAX_CHARS:
                    reply = reply[:GUEST_RESPONSE_MAX_CHARS - 3] + "..."
                if len(reply) >= 15:
                    return reply
        except Exception as e:
            logger.debug(f"Local model guest reply failed: {e}")

    # Fallback — short static reply
    return (
        "Привет! Это Ася — автоэксперт. Сейчас работаю в гостевом режиме и не могу "
        "полностью развернуть ответ. Напиши мне в личку @asiaexp_bot — там отвечу как надо 🚗"
    )


async def _build_guest_shop_keyboard(text: str) -> Optional[InlineKeyboardMarkup]:
    """Build an inline keyboard with up to 4 product cards relevant to a guest message.

    Reuses the same logic as channel._build_topic_shop_keyboard, but inlined
    here to avoid a circular import (channel.py imports bot.shop, but if
    channel.py imports this module it's fine — we just need the DB call).
    """
    cat = category_for_text(text)
    if not cat:
        return None
    products = await get_unposted_shop_products(
        category_label=cat["label"],
        limit=4,
        randomize=True,
    )
    if len(products) < 2:
        return None

    # Build inline keyboard markup as a plain dict (no aiogram types needed
    # because we send via raw HTTP for answerGuestQuery)
    buttons = []
    for p in products:
        price_int = int(p["price"]) if p["price"] else 0
        name = (p["name"] or "Товар")[:40]
        brand = p["brand"] or ""
        label = f"🛒 {brand} {name} — {price_int}₽".strip()
        if len(label) > 60:
            label = label[:57] + "..."
        url = p["affiliate_url"] or p["product_url"]
        if url:
            buttons.append({"text": label, "url": url})

    buttons.append({
        "text": f"🛍️ Все товары: {cat['label']}",
        "url": "https://sochiautoparts.ru/shop",
    })

    # Return as InlineKeyboardMarkup-shaped dict (one button per row)
    return {
        "inline_keyboard": [[b] for b in buttons],
    }


# ── Guest message processor ───────────────────────────────────────────────────

async def process_guest_message(bot: Bot, guest_message: Dict[str, Any]) -> bool:
    """Process a single guest_message update from Telegram.

    guest_message structure (Bot API 10.0):
    {
        "message_id": int,
        "from": {...},                # User who triggered the guest message
        "chat": {...},                # Chat where the message was sent (bot is NOT a member)
        "guest_query_id": str,        # ID to use with answerGuestQuery
        "guest_bot_caller_user": {...},   # The user who initiated the bot call
        "guest_bot_caller_chat": {...},   # The chat where the bot was called from
        "date": int,
        "text": str                   # Message text (if any)
    }

    Returns True if a reply was sent.
    """
    guest_query_id = guest_message.get("guest_query_id", "")
    if not guest_query_id:
        logger.debug("guest_message without guest_query_id, skipping")
        return False

    if _is_already_answered(guest_query_id):
        logger.debug(f"Already answered guest_query_id={guest_query_id[:16]}...")
        return False

    text = guest_message.get("text", "") or ""
    if not text:
        # Non-text guest messages — we can't do much, send a generic reply
        text = "(вопрос без текста)"

    # Truncate very long inputs (avoid token blowup)
    if len(text) > 1500:
        text = text[:1500] + "…"

    logger.info(
        f"Guest message from chat={guest_message.get('chat', {}).get('id', '?')}: "
        f"{text[:80]}..."
    )

    # Generate response (LOCAL MODEL)
    reply_text = await _generate_guest_response(text)

    # Try to attach product cards if topic matches a shop category
    reply_markup = await _build_guest_shop_keyboard(text)

    # Send answerGuestQuery via raw HTTP
    payload = {
        "guest_query_id": guest_query_id,
        "text": reply_text,
        "parse_mode": "HTML",
        "disable_notification": False,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    result = await call_bot_api_raw(config.BOT_TOKEN, "answerGuestQuery", payload)

    if result.get("ok"):
        logger.info(f"Guest query answered (id={guest_query_id[:16]}...)")
        return True
    else:
        logger.warning(
            f"Guest query answer FAILED: {result.get('error_code')} "
            f"{result.get('description', '')[:200]}"
        )
        return False


# ── Middleware ────────────────────────────────────────────────────────────────

class GuestModeMiddleware(BaseMiddleware):
    """Outer middleware on dp.update — intercepts guest_message updates.

    aiogram 3.15 doesn't know about the `guest_message` field on Update
    (added in Bot API 10.0), but Pydantic's `extra='allow'` config keeps
    unknown fields in `update.model_extra`. We inspect that dict here.

    If a guest_message is found, we process it and STILL pass the update
    to the next handler (so other middlewares / handlers run normally —
    they'll just see no message/channel_post/etc. and ignore it).
    """

    async def __call__(self, handler, event: Update, data: Dict[str, Any]):
        try:
            # Get the bot from data (aiogram injects it)
            bot: Bot = data.get("bot") or data.get("bot_instance")
            if not bot:
                # Fallback: try to find the bot in the middleware context
                bot = getattr(self, "_bot", None)
            if not bot:
                return await handler(event, data)

            # Inspect model_extra for guest_message
            extra = getattr(event, "model_extra", None) or {}
            guest_msg = extra.get("guest_message")
            if guest_msg and isinstance(guest_msg, dict):
                # Process in background — don't block the dispatcher
                asyncio.create_task(process_guest_message(bot, guest_msg))

        except Exception as e:
            logger.debug(f"GuestModeMiddleware error (non-critical): {e}")

        # Always pass through to the next handler
        return await handler(event, data)


def attach_guest_mode(dp, bot: Bot) -> None:
    """Register the Guest Mode middleware on a Dispatcher.

    Usage in bot/main.py:
        from bot.guest_mode import attach_guest_mode
        attach_guest_mode(dp, bot)
    """
    try:
        dp.update.outer_middleware(GuestModeMiddleware())
        # Stash the bot on the middleware class so handlers can find it
        GuestModeMiddleware._bot = bot
        logger.info("Guest Mode middleware attached (Bot API 10.0)")
    except Exception as e:
        logger.warning(f"Failed to attach Guest Mode middleware: {e}")
