"""
Channel Manager — Posts to @sochiautoparts with Asya's footer.
Handles news posts, partner posts, scheduled content, reactions, comments, and media.
"""

import logging
import time
import random
import asyncio
from typing import Optional, List, Dict
from datetime import datetime

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.types import InputFile, FSInputFile, ReactionTypeEmoji

from bot.config import config, persona
from bot.database import (
    add_channel_post, get_today_post_count, get_unposted_news,
    mark_news_posted, add_partner_post, get_today_partner_post_count,
)
from ai.router import ai_router
from bot.partners import partner_manager

logger = logging.getLogger("asya.channel")

# ── Reactions to add to posts ───────────────────────────────────────────────

POST_REACTIONS = ["👍", "🔥", "🚗", "😍", "👏", "💯", "🤩", "⚡"]

# ── Asya's comment phrases for posts ────────────────────────────────────────

ASYA_COMMENTS = [
    "Классная новость! Давно ждала чего-то подобного 🚗",
    "Вот это да! Неожиданно, но интересно 🔥",
    "Слежу за этой темой — будет ещё много новостей!",
    "Мне нравится, куда движется автопром. А вам? 👀",
    "Интересная тема для обсуждения! Пишите в @asiaexp_bot 💬",
    "Это точно стоит внимания! Подробности у меня в @asiaexp_bot",
    "Автомир не стоит на месте! Свежие новости каждый день 🏎️",
    "Супер! Если хотите обсудить — жду в личке @asiaexp_bot 💬",
    "Трендовая тема! Спрашивайте подробности у меня 🚗",
    "Вот это поворот! Кто что думает? Пишите @asiaexp_bot",
]


class ChannelManager:
    """Manages posting to the @sochiautoparts channel."""

    def __init__(self):
        self._bot: Optional[Bot] = None
        self._last_post_time: float = 0
        self._last_partner_time: float = 0

    def set_bot(self, bot: Bot) -> None:
        """Set the bot instance for sending messages."""
        self._bot = bot

    async def _add_reaction(self, chat_id, message_id: int) -> None:
        """Add a reaction to a post from the channel account."""
        try:
            emoji = random.choice(POST_REACTIONS)
            await self._bot.set_message_reaction(
                chat_id=chat_id,
                message_id=message_id,
                reaction=[ReactionTypeEmoji(emoji=emoji)],
            )
            logger.info(f"Added reaction {emoji} to message {message_id}")
        except Exception as e:
            logger.debug(f"Could not add reaction: {e}")

    async def _add_comment(self, chat_id, message_id: int, news_title: str = "") -> None:
        """Add a comment on behalf of the channel to the post."""
        try:
            comment = random.choice(ASYA_COMMENTS)
            await self._bot.send_message(
                chat_id=chat_id,
                text=comment,
                reply_to_message_id=message_id,
            )
            logger.info(f"Added comment to message {message_id}")
        except Exception as e:
            logger.debug(f"Could not add comment: {e}")

    async def _generate_post_image(self, news_title: str) -> Optional[bytes]:
        """Generate an image for a news post using AI."""
        try:
            # Create a prompt for image generation based on the news topic
            image_prompt = (
                f"Automotive news illustration: {news_title}. "
                f"Professional automotive photography style, modern car, "
                f"vibrant colors, high quality, dramatic lighting, "
                f"magazine cover style, no text overlay."
            )
            image_data = await ai_router._primary.generate_image(image_prompt, model="flux")
            return image_data
        except Exception as e:
            logger.error(f"Image generation failed: {e}")
            return None

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

        # Ensure footer with proper format: [Ася - Автоэксперт](https://t.me/asiaexp_bot) + @sochiautoparts + #sochiautoparts
        if "#sochiautoparts" not in post_text:
            post_text = post_text.rstrip() + "\n\n#sochiautoparts"
        if "asiaexp_bot" not in post_text:
            post_text = post_text.rstrip() + f"\n\n[Ася - Автоэксперт](https://t.me/asiaexp_bot)\n@sochiautoparts"
        if "@sochiautoparts" not in post_text:
            post_text = post_text.rstrip() + "\n@sochiautoparts"

        # Try to generate and attach an image
        image_data = None
        try:
            image_data = await self._generate_post_image(news_item["title"])
        except Exception as e:
            logger.warning(f"Image generation skipped: {e}")

        # Post to channel
        try:
            import tempfile
            import os

            if image_data:
                # Save image to temp file and send with caption
                tmp_path = os.path.join(tempfile.gettempdir(), f"asya_post_{int(time.time())}.png")
                with open(tmp_path, "wb") as f:
                    f.write(image_data)

                photo = FSInputFile(tmp_path, filename="asya_post.png")
                sent = await self._bot.send_photo(
                    chat_id=config.CHANNEL_ID,
                    photo=photo,
                    caption=post_text[:1024],  # Telegram caption limit
                    parse_mode=ParseMode.MARKDOWN,
                )

                # Clean up temp file
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            else:
                # Send text-only post
                sent = await self._bot.send_message(
                    chat_id=config.CHANNEL_ID,
                    text=post_text,
                    parse_mode=ParseMode.MARKDOWN,
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

            # Add reaction to own post
            await self._add_reaction(config.CHANNEL_ID, sent.message_id)

            # Add comment from channel
            await self._add_comment(config.CHANNEL_ID, sent.message_id, news_item.get("title", ""))

            logger.info(f"Posted news to channel: {news_item['title'][:50]}...")
            return True

        except Exception as e:
            logger.error(f"Error posting to channel: {e}")
            # Try sending without markdown if parsing failed
            try:
                sent = await self._bot.send_message(
                    chat_id=config.CHANNEL_ID,
                    text=post_text,
                )
                await add_channel_post(
                    content=post_text,
                    message_id=sent.message_id,
                    post_type="news",
                    source_url=news_item.get("url", ""),
                )
                if news_item.get("url"):
                    await mark_news_posted(news_item["url"])
                self._last_post_time = time.time()
                await self._add_reaction(config.CHANNEL_ID, sent.message_id)
                await self._add_comment(config.CHANNEL_ID, sent.message_id)
                return True
            except Exception as e2:
                logger.error(f"Error posting without markdown: {e2}")
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
                "Напиши естественно, как живая девушка-автоэксперт, которая советует проверенный сервис. "
                "Не делай это откровенной рекламой — вставь рекомендацию органично. "
                f"Ссылка: {partner_manager.format_affiliate_link(program)}"
            ),
        )

        if not response.error and response.text:
            post_content = response.text

        # Ensure footer with proper format
        if "#sochiautoparts" not in post_content:
            post_content = post_content.rstrip() + "\n\n#sochiautoparts"
        if "asiaexp_bot" not in post_content:
            post_content = post_content.rstrip() + f"\n\n[Ася - Автоэксперт](https://t.me/asiaexp_bot)\n@sochiautoparts"
        if "@sochiautoparts" not in post_content:
            post_content = post_content.rstrip() + "\n@sochiautoparts"

        try:
            sent = await self._bot.send_message(
                chat_id=config.CHANNEL_ID,
                text=post_content,
                parse_mode=ParseMode.MARKDOWN,
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

            # Add reaction and comment
            await self._add_reaction(config.CHANNEL_ID, sent.message_id)
            await self._add_comment(config.CHANNEL_ID, sent.message_id)

            logger.info(f"Posted partner content: {program.name}")
            return True

        except Exception as e:
            logger.error(f"Error posting partner content: {e}")
            # Retry without markdown
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
                await self._add_reaction(config.CHANNEL_ID, sent.message_id)
                return True
            except Exception as e2:
                logger.error(f"Error posting partner content without markdown: {e2}")
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
