"""
Channel Manager — Posts to @sochiautoparts with Asya's footer.
Handles news posts, partner posts, and scheduled content.
"""

import logging
import time
import random
from typing import Optional, List, Dict

from aiogram import Bot
from aiogram.enums import ParseMode

from bot.config import config, persona
from bot.database import (
    add_channel_post, get_today_post_count, get_unposted_news,
    mark_news_posted, add_partner_post, get_today_partner_post_count,
)
from ai.router import ai_router
from bot.partners import partner_manager

logger = logging.getLogger("asya.channel")


class ChannelManager:
    """Manages posting to the @sochiautoparts channel."""

    def __init__(self):
        self._bot: Optional[Bot] = None
        self._last_post_time: float = 0
        self._last_partner_time: float = 0

    def set_bot(self, bot: Bot) -> None:
        """Set the bot instance for sending messages."""
        self._bot = bot

    async def post_news(self, news_item: Optional[Dict] = None) -> bool:
        """
        Post a news item to the channel.
        If no item specified, picks the best unposted news.
        """
        if not self._bot:
            logger.error("Bot not set in ChannelManager")
            return False

        # Check daily limit
        today_count = await get_today_post_count()
        if today_count >= config.CHANNEL_MAX_POSTS_PER_DAY:
            logger.info("Daily post limit reached")
            return False

        # Check minimum interval
        min_interval = config.CHANNEL_POST_INTERVAL_MINUTES * 60
        if time.time() - self._last_post_time < min_interval:
            logger.info("Post interval too short")
            return False

        # Get news item if not provided
        if not news_item:
            unposted = await get_unposted_news(limit=5)
            if not unposted:
                logger.info("No unposted news available")
                return False

            # Prefer auto category, then tech, then general
            auto_news = [n for n in unposted if n.get("category") == "auto"]
            if auto_news:
                news_item = random.choice(auto_news)
            else:
                news_item = unposted[0]

        # Generate post content using AI
        source_text = ""
        if news_item.get("summary"):
            source_text = news_item["summary"]

        # For international news, add translation instruction
        extra_instructions = ""
        if news_item.get("lang") != "ru":
            extra_instructions = (
                "Это новость из зарубежного источника. "
                "Переведи на русский язык и адаптируй для русскоязычной аудитории. "
                "Сохрани суть и факты, но напиши естественно на русском."
            )

        response = await ai_router.generate_channel_post(
            topic=news_item["title"],
            source_text=source_text,
            extra_instructions=extra_instructions,
        )

        if response.error or not response.text:
            logger.error(f"Failed to generate channel post: {response.error_message}")
            return False

        post_text = response.text

        # Ensure footer
        if "@sochiautoparts" not in post_text:
            post_text = post_text.rstrip() + f"\n\nАся - Автоэксперт\n@sochiautoparts"

        # Ensure "Ася - Автоэксперт" line is there
        if "Ася - Автоэксперт" not in post_text and "Ася — Автоэксперт" not in post_text:
            post_text = post_text.rstrip() + "\n\nАся - Автоэксперт\n@sochiautoparts"

        # Post to channel
        try:
            sent = await self._bot.send_message(
                chat_id=config.CHANNEL_ID,
                text=post_text,
            )

            # Record in database
            await add_channel_post(
                content=post_text,
                message_id=sent.message_id,
                post_type="news",
                source_url=news_item.get("url", ""),
            )

            # Mark news as posted
            if news_item.get("url"):
                await mark_news_posted(news_item["url"])

            self._last_post_time = time.time()
            logger.info(f"Posted news to channel: {news_item['title'][:50]}...")
            return True

        except Exception as e:
            logger.error(f"Error posting to channel: {e}")
            return False

    async def post_partner_content(self) -> bool:
        """
        Post a partner/admitad post to the channel.
        Only posts if within daily limits and interval.
        """
        if not self._bot:
            logger.error("Bot not set in ChannelManager")
            return False

        # Check if partner posting is allowed
        if not partner_manager.should_post_partner():
            return False

        # Check daily post limit
        today_count = await get_today_post_count()
        if today_count >= config.CHANNEL_MAX_POSTS_PER_DAY:
            return False

        # Get a random partner program
        program = partner_manager.get_random_program()
        if not program:
            logger.info("No partner programs available")
            return False

        # Generate partner post content
        post_content = await partner_manager.generate_partner_post_content(program)

        # Enhance with AI
        response = await ai_router.generate_channel_post(
            topic=f"Рекомендация сервиса: {program.name}",
            source_text=f"Партнёрская программа: {program.name}. Описание: {program.description or 'Автомобильный сервис'}",
            extra_instructions=(
                "Это партнёрский пост — рекомендация сервиса для автомобилистов. "
                "Напиши естественно, как автоэксперт, который советует проверенный сервис. "
                "Не делай это откровенной рекламой — вставь рекомендацию органично. "
                f"Ссылка: {partner_manager.format_affiliate_link(program)}"
            ),
        )

        if not response.error and response.text:
            post_content = response.text

        # Ensure footer
        if "@sochiautoparts" not in post_content:
            post_content = post_content.rstrip() + f"\n\nАся - Автоэксперт\n@sochiautoparts"

        try:
            sent = await self._bot.send_message(
                chat_id=config.CHANNEL_ID,
                text=post_content,
            )

            await add_partner_post(
                program_id=program.id,
                program_name=program.name,
                category=", ".join(program.categories) if program.categories else "general",
                affiliate_url=program.goto_link,
                post_content=post_content,
                message_id=sent.message_id,
            )

            await add_channel_post(
                content=post_content,
                message_id=sent.message_id,
                post_type="partner",
                partner_program=program.name,
            )

            partner_manager.mark_posted()
            self._last_partner_time = time.time()
            logger.info(f"Posted partner content: {program.name}")
            return True

        except Exception as e:
            logger.error(f"Error posting partner content: {e}")
            return False

    async def run_scheduled_post(self) -> bool:
        """
        Run a scheduled post — either news or partner content.
        Intelligently chooses what to post based on timing and content availability.
        """
        # Check if it's time for a partner post
        now = time.time()
        partner_interval = config.PARTNER_POST_INTERVAL_HOURS * 3600

        if (now - self._last_partner_time >= partner_interval and
                partner_manager.should_post_partner()):
            # Alternate between partner and news
            if random.random() < 0.3:  # 30% chance for partner post
                return await self.post_partner_content()

        # Default: post news
        return await self.post_news()


# ── Global instance ────────────────────────────────────────────────────────────

channel_manager = ChannelManager()
