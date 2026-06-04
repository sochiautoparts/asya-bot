"""
Admin Handler — Admin-only commands for managing the bot.
"""

import logging
from typing import Optional

from aiogram import Router, F, types
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.enums import ChatAction

from bot.config import config
from bot.database import (
    is_user_admin, set_user_admin, block_user,
    get_stats, get_today_post_count, get_today_partner_post_count,
    get_unposted_news,
)
from ai.router import ai_router
from bot.partners import partner_manager
from bot.web_search import web_search, format_search_results
from channel import channel_manager

logger = logging.getLogger("asya.handlers.admin")

admin_router = Router()


# ── Admin check filter ─────────────────────────────────────────────────────────

async def _is_admin(message: Message) -> bool:
    """Check if the message sender is an admin."""
    return await is_user_admin(message.from_user.id)


# ── /admin command ─────────────────────────────────────────────────────────────

@admin_router.message(Command("admin"))
async def cmd_admin(message: Message):
    """Show admin panel."""
    if not await _is_admin(message):
        await message.answer("У вас нет прав администратора.")
        return

    stats = await get_stats()
    today_posts = await get_today_post_count()
    today_partner = await get_today_partner_post_count()

    text = (
        f"🛠️ Панель администратора Asya Bot\n\n"
        f"📊 Статистика:\n"
        f"  Пользователей: {stats['total_users']}\n"
        f"  Активных: {stats['active_users']}\n"
        f"  Новостей в базе: {stats['total_news']}\n"
        f"  Непостоянных новостей: {stats['unposted_news']}\n"
        f"  Постов в канале: {stats['total_posts']}\n"
        f"  Партнёрских постов: {stats['partner_posts']}\n"
        f"  Сегодня постов: {today_posts}\n"
        f"  Сегодня партнёрских: {today_partner}\n"
        f"  Кэшированных запросов: {stats['cached_queries']}\n\n"
        f"Команды:\n"
        f"/status — статус бота\n"
        f"/post — создать пост в канал\n"
        f"/partner_post — партнёрский пост\n"
        f"/news — показать непостоянные новости\n"
        f"/search <запрос> — веб-поиск\n"
        f"/addadmin <user_id> — добавить админа\n"
        f"/block <user_id> — заблокировать пользователя\n"
        f"/unblock <user_id> — разблокировать\n"
        f"/models — список AI моделей\n"
        f"/switch <модель> — переключить AI модель\n"
        f"/reload_partners — перезагрузить партнёров"
    )
    await message.answer(text)


# ── /status command ────────────────────────────────────────────────────────────

@admin_router.message(Command("status"))
async def cmd_status(message: Message):
    """Show bot status."""
    if not await _is_admin(message):
        return

    import asyncio
    from datetime import datetime

    is_ai = await ai_router.primary.is_available() if ai_router.primary else False
    partner_count = len(partner_manager.programs)
    unposted = await get_unposted_news(limit=1)

    text = (
        f"✅ Asya Bot работает\n\n"
        f"🤖 AI провайдер: {'доступен' if is_ai else 'недоступен'}\n"
        f"📰 Партнёрских программ: {partner_count}\n"
        f"📝 Непостоянных новостей: {len(unposted)}\n"
        f"⏰ Время: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    await message.answer(text)


# ── /post command ──────────────────────────────────────────────────────────────

@admin_router.message(Command("post"))
async def cmd_post(message: Message):
    """Create a post in the channel."""
    if not await _is_admin(message):
        return

    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    # Get the topic from command args
    args = message.text.split(maxsplit=1)
    topic = args[1] if len(args) > 1 else ""

    if not topic:
        # Pick from unposted news
        news = await get_unposted_news(limit=5)
        if not news:
            await message.answer("Нет непостоянных новостей для поста. Укажите тему: /post <тема>")
            return
        # Pick a random one
        import random
        item = random.choice(news)
        topic = item["title"]
        source_url = item["url"]
        source_summary = item["summary"]
    else:
        source_url = ""
        source_summary = ""

    # Generate post using AI
    response = await ai_router.generate_channel_post(
        topic=topic,
        source_text=source_summary,
    )

    if response.error:
        await message.answer(f"Ошибка генерации поста: {response.error_message}")
        return

    # Show preview to admin
    preview = f"📝 Предпросмотр поста:\n\n{response.text}\n\nОтправить в канал? /send_post"
    await message.answer(preview)

    # Store the post content for /send_post
    message.bot._pending_post = response.text
    message.bot._pending_source_url = source_url


# ── /send_post command ────────────────────────────────────────────────────────

@admin_router.message(Command("send_post"))
async def cmd_send_post(message: Message):
    """Send the pending post to the channel."""
    if not await _is_admin(message):
        return

    post_text = getattr(message.bot, "_pending_post", None)
    source_url = getattr(message.bot, "_pending_source_url", "")

    if not post_text:
        await message.answer("Нет поста для отправки. Сначала создайте через /post")
        return

    try:
        sent = await message.bot.send_message(
            chat_id=config.CHANNEL_ID,
            text=post_text,
        )

        from bot.database import add_channel_post, mark_news_posted
        await add_channel_post(
            content=post_text,
            message_id=sent.message_id,
            post_type="news",
            source_url=source_url,
        )

        if source_url:
            await mark_news_posted(source_url)

        await message.answer(f"✅ Пост опубликован в {config.CHANNEL_ID}")

        # Clear pending
        message.bot._pending_post = None
        message.bot._pending_source_url = ""

    except Exception as e:
        logger.error(f"Error sending post to channel: {e}")
        await message.answer(f"❌ Ошибка публикации: {e}")


# ── /partner_post command ─────────────────────────────────────────────────────

@admin_router.message(Command("partner_post"))
async def cmd_partner_post(message: Message):
    """Create a partner post for the channel."""
    if not await _is_admin(message):
        return

    args = message.text.split(maxsplit=1)
    category = args[1] if len(args) > 1 else ""

    program = partner_manager.get_random_program(category=category)
    if not program:
        await message.answer("Партнёрские программы не загружены или не найдены для указанной категории.")
        return

    post_content = await partner_manager.generate_partner_post_content(program)

    await message.answer(f"📝 Предпросмотр партнёрского поста:\n\n{post_content}\n\nОтправить? /send_partner_post")
    message.bot._pending_partner_post = post_content
    message.bot._pending_partner_program = program


# ── /send_partner_post command ────────────────────────────────────────────────

@admin_router.message(Command("send_partner_post"))
async def cmd_send_partner_post(message: Message):
    """Send the pending partner post to the channel."""
    if not await _is_admin(message):
        return

    post_text = getattr(message.bot, "_pending_partner_post", None)
    program = getattr(message.bot, "_pending_partner_program", None)

    if not post_text or not program:
        await message.answer("Нет партнёрского поста для отправки.")
        return

    try:
        sent = await message.bot.send_message(
            chat_id=config.CHANNEL_ID,
            text=post_text,
        )

        from bot.database import add_partner_post
        await add_partner_post(
            program_id=program.id,
            program_name=program.name,
            category=", ".join(program.categories) if program.categories else "general",
            affiliate_url=program.goto_link,
            post_content=post_text,
            message_id=sent.message_id,
        )

        partner_manager.mark_posted()
        await message.answer(f"✅ Партнёрский пост опубликован: {program.name}")

        # Clear pending
        message.bot._pending_partner_post = None
        message.bot._pending_partner_program = None

    except Exception as e:
        logger.error(f"Error sending partner post: {e}")
        await message.answer(f"❌ Ошибка публикации: {e}")


# ── /news command ─────────────────────────────────────────────────────────────

@admin_router.message(Command("news"))
async def cmd_news(message: Message):
    """Show unposted news items."""
    if not await _is_admin(message):
        return

    news = await get_unposted_news(limit=10)
    if not news:
        await message.answer("Нет непостоянных новостей.")
        return

    lines = ["📰 Непостоянные новости:\n"]
    for i, item in enumerate(news[:10], 1):
        lines.append(f"{i}. {item['title']}")
        lines.append(f"   {item['url']}")
        lines.append(f"   Источник: {item['source']} | Категория: {item['category']}\n")

    await message.answer("\n".join(lines))


# ── /search command ────────────────────────────────────────────────────────────

@admin_router.message(Command("search"))
async def cmd_search(message: Message):
    """Perform a web search (admin only)."""
    if not await _is_admin(message):
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Использование: /search <запрос>")
        return

    query = args[1]
    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    results = await web_search(query, max_results=5)
    text = format_search_results(results, max_items=5)
    await message.answer(text)


# ── /addadmin command ─────────────────────────────────────────────────────────

@admin_router.message(Command("addadmin"))
async def cmd_addadmin(message: Message):
    """Add a user as admin."""
    if message.from_user.id != config.OWNER_ID:
        await message.answer("Только владелец может добавлять админов.")
        return

    args = message.text.split()
    if len(args) < 2:
        await message.answer("Использование: /addadmin <user_id>")
        return

    try:
        target_id = int(args[1])
    except ValueError:
        await message.answer("Неверный user_id. Должно быть число.")
        return

    await set_user_admin(target_id, True)
    await message.answer(f"✅ Пользователь {target_id} теперь админ.")


# ── /block and /unblock commands ──────────────────────────────────────────────

@admin_router.message(Command("block"))
async def cmd_block(message: Message):
    """Block a user."""
    if not await _is_admin(message):
        return

    args = message.text.split()
    if len(args) < 2:
        await message.answer("Использование: /block <user_id>")
        return

    try:
        target_id = int(args[1])
    except ValueError:
        await message.answer("Неверный user_id.")
        return

    await block_user(target_id, True)
    await message.answer(f"🚫 Пользователь {target_id} заблокирован.")


@admin_router.message(Command("unblock"))
async def cmd_unblock(message: Message):
    """Unblock a user."""
    if not await _is_admin(message):
        return

    args = message.text.split()
    if len(args) < 2:
        await message.answer("Использование: /unblock <user_id>")
        return

    try:
        target_id = int(args[1])
    except ValueError:
        await message.answer("Неверный user_id.")
        return

    await block_user(target_id, False)
    await message.answer(f"✅ Пользователь {target_id} разблокирован.")


# ── /models command ───────────────────────────────────────────────────────────

@admin_router.message(Command("models"))
async def cmd_models(message: Message):
    """Show available AI models grouped by provider."""
    if not await _is_admin(message):
        return

    models = ai_router.get_available_models()
    # Group by category (only tested & working models)
    categories = {
        "OpenAI": ["openai", "openai-fast", "openai-large", "gpt-5.4-mini", "gpt-5.5"],
        "Mistral": ["mistral", "mistral-large", "mistral-4"],
        "DeepSeek": ["deepseek", "deepseek-pro"],
        "Qwen": ["qwen-coder", "qwen-vision", "qwen-vision-pro"],
        "Llama": ["llama", "llama-scout"],
        "Nova": ["nova", "nova-fast"],
        "Grok": ["grok"],
        "Perplexity": ["perplexity", "perplexity-fast", "perplexity-deep", "perplexity-reasoning"],
        "Other": ["gemma", "glm", "minimax", "minimax-m3"],
        "Image": ["flux", "gptimage", "gptimage-large", "kontext", "zimage", "nova-canvas"],
        "Audio": ["whisper", "universal-2", "universal-3-pro"],
    }

    lines = ["🤖 Доступные AI модели:\n"]
    for cat, cat_models in categories.items():
        cat_available = [m for m in cat_models if m in models]
        if cat_available:
            lines.append(f"📋 {cat}:")
            for m in cat_available:
                lines.append(f"  • {m}")

    lines.append("\n/switch <модель> — переключить модель")
    await message.answer("\n".join(lines))


# ── /switch command ───────────────────────────────────────────────────────────

@admin_router.message(Command("switch"))
async def cmd_switch_model(message: Message):
    """Switch the default AI model for chat."""
    if not await _is_admin(message):
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Использование: /switch <модель>\nПример: /switch mistral-4")
        return

    model_name = args[1].strip()
    available = ai_router.get_available_models()

    if model_name not in available:
        await message.answer(f"Модель '{model_name}' не найдена. Используйте /models для списка.")
        return

    # Update default model in provider
    from ai.providers.pollinations_provider import DEFAULT_MODEL
    import ai.providers.pollinations_provider as pp
    pp.DEFAULT_MODEL = model_name

    await message.answer(f"✅ Модель переключена на: {model_name}")


# ── /reload_partners command ──────────────────────────────────────────────────

@admin_router.message(Command("reload_partners"))
async def cmd_reload_partners(message: Message):
    """Reload partner programs from JSON file."""
    if not await _is_admin(message):
        return

    count = partner_manager.load()
    await message.answer(f"✅ Загружено {count} партнёрских программ.")
