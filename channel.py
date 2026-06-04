"""
Channel Manager — Posts to @sochiautoparts with Asya's footer.
Handles news posts, partner posts, scheduled content, reactions, comments,
media, polls, and internet news search.
"""

import logging
import time
import random
import asyncio
import tempfile
import os
from typing import Optional, List, Dict
from datetime import datetime

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.types import FSInputFile, ReactionTypeEmoji
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardButton

from bot.config import config, persona
from bot.database import (
    add_channel_post, get_today_post_count, get_unposted_news,
    mark_news_posted, add_partner_post, get_today_partner_post_count,
)
from ai.router import ai_router
from bot.partners import partner_manager
from bot.web_search import web_search, search_news, SearchResult

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
    "Мнения разделились, но мне нравится! А вам? 🤔",
    "Эта тема точно заслуживает внимания! 📣",
]

# ── Poll topics for channel engagement ──────────────────────────────────────

POLL_TEMPLATES = [
    "Что думаете об этой новости?",
    "Ваше мнение?",
    "Как вам такая новость?",
    "Оцените новость!",
    "Что скажете?",
]


class ChannelManager:
    """Manages posting to the @sochiautoparts channel."""

    def __init__(self):
        self._bot: Optional[Bot] = None
        self._last_post_time: float = 0
        self._last_partner_time: float = 0
        self._last_poll_time: float = 0
        self._poll_count: int = 0

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

    async def _download_partner_image(self, image_url: str) -> Optional[bytes]:
        """Download a partner program image (logo/banner)."""
        if not image_url:
            return None
        try:
            import httpx
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                response = await client.get(image_url)
                if response.status_code == 200 and len(response.content) > 500:
                    # Only accept actual image data (not tiny SVGs or errors)
                    content_type = response.headers.get("content-type", "")
                    if "image" in content_type or len(response.content) > 5000:
                        return response.content
        except Exception as e:
            logger.debug(f"Could not download partner image: {e}")
        return None

    async def _create_poll_from_news(self, news_title: str, news_summary: str = "") -> Optional[Dict]:
        """Generate a poll question and options based on a news item using AI."""
        try:
            response = await ai_router._primary.chat(
                messages=[
                    {"role": "system", "content": (
                        "Ты создаёшь опросы для автоканала в Telegram. "
                        "На основе новости создай опрос: вопрос и 4 варианта ответа. "
                        "Формат строго JSON: {\"question\": \"...\", \"options\": [\"...\", \"...\", \"...\", \"...\"]}. "
                        "Никакого текста кроме JSON. Вопрос короткий и живой. "
                        "Варианты ответа короткие (до 20 символов)."
                    )},
                    {"role": "user", "content": f"Новость: {news_title}\n{news_summary[:300]}"},
                ],
                model="openai-fast",
                temperature=0.8,
                max_tokens=200,
            )

            if response.error or not response.text:
                return None

            # Parse JSON from response
            import json
            text = response.text.strip()
            # Try to extract JSON from response
            if "```" in text:
                text = text.split("```")[1].strip()
                if text.startswith("json"):
                    text = text[4:].strip()

            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                data = json.loads(text[start:end])
                question = data.get("question", "")
                options = data.get("options", [])
                if question and len(options) >= 2:
                    return {"question": question, "options": options[:4]}

        except Exception as e:
            logger.error(f"Poll generation error: {e}")

        return None

    async def post_news(self, news_item: Optional[Dict] = None) -> bool:
        """
        Post a news item to the channel.
        If no item specified, picks the best unposted news or searches internet.
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
            if unposted:
                # Prefer auto category, then tech, then general
                auto_news = [n for n in unposted if n.get("category") == "auto"]
                if auto_news:
                    news_item = random.choice(auto_news)
                else:
                    news_item = unposted[0]
            else:
                # No unposted RSS news — search internet for fresh auto news
                news_item = await self._search_internet_news()
                if not news_item:
                    logger.info("No unposted news available (RSS or internet)")
                    return False

        # Generate post content using AI
        source_text = ""
        if news_item.get("summary"):
            source_text = news_item["summary"]

        # For international news, add translation instruction
        extra_instructions = (
            "Уникализируй текст — перепиши своими словами, сохранив факты. "
            "Не копируй оригинальные формулировки. "
            "Добавь своё мнение и эмоции как живой девушки-автоэксперта. "
        )
        if news_item.get("lang") and news_item.get("lang") != "ru":
            extra_instructions += (
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

        # Ensure footer with proper format
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
            if image_data:
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

                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            else:
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

            # Occasionally create a poll based on the news (every 3rd post)
            self._poll_count += 1
            if self._poll_count % 3 == 0:
                asyncio.create_task(self._post_poll(news_item))

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

    async def _search_internet_news(self) -> Optional[Dict]:
        """Search the internet for fresh auto news when RSS feeds are empty."""
        try:
            # Search for latest auto news
            queries = [
                "автомобильные новости сегодня 2024 2025 2026",
                "новости автопрома новинки авто",
                "auto news latest today",
            ]
            query = random.choice(queries)
            results = await web_search(query, max_results=5)

            if not results:
                return None

            # Pick a random result
            result = random.choice(results)
            if not result.title:
                return None

            news_item = {
                "title": result.title,
                "url": result.url,
                "summary": result.snippet or "",
                "category": "auto",
                "lang": "ru" if any(c >= '\u0400' for c in result.title) else "en",
            }

            logger.info(f"Found internet news: {news_item['title'][:50]}")
            return news_item

        except Exception as e:
            logger.error(f"Internet news search error: {e}")
            return None

    async def _post_poll(self, news_item: Dict) -> None:
        """Create a poll in the channel based on a news item."""
        try:
            # Don't create polls too frequently
            now = time.time()
            if now - self._last_poll_time < 3600:  # At least 1 hour between polls
                return

            poll_data = await self._create_poll_from_news(
                news_item.get("title", ""),
                news_item.get("summary", ""),
            )

            if not poll_data:
                return

            question = poll_data["question"]
            options = poll_data["options"]

            await self._bot.send_poll(
                chat_id=config.CHANNEL_ID,
                question=f"📊 {question}",
                options=options,
                is_anonymous=True,
            )

            self._last_poll_time = time.time()
            logger.info(f"Posted poll: {question}")

        except Exception as e:
            logger.error(f"Poll posting error: {e}")

    async def post_partner_content(self) -> bool:
        """
        Post a partner/admitad post to the channel with partner image.
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

        # Try to use partner image from admitad data
        partner_image_url = program.image if hasattr(program, 'image') else None
        partner_image_data = None
        if partner_image_url:
            try:
                partner_image_data = await self._download_partner_image(partner_image_url)
            except Exception as e:
                logger.debug(f"Partner image download failed: {e}")

        # If no partner image, try AI-generated image
        if not partner_image_data:
            try:
                partner_image_data = await self._generate_post_image(f"{program.name} automotive service")
            except Exception:
                pass

        try:
            if partner_image_data:
                # Determine extension
                ext = ".png"
                if partner_image_url and ".jpg" in partner_image_url.lower():
                    ext = ".jpg"
                elif partner_image_url and ".svg" in partner_image_url.lower():
                    ext = ".png"  # We'll send as-is, Telegram handles it

                tmp_path = os.path.join(tempfile.gettempdir(), f"asya_partner_{int(time.time())}{ext}")
                with open(tmp_path, "wb") as f:
                    f.write(partner_image_data)

                photo = FSInputFile(tmp_path, filename=f"partner_{program.name[:20]}.{ext.lstrip('.')}")
                sent = await self._bot.send_photo(
                    chat_id=config.CHANNEL_ID,
                    photo=photo,
                    caption=post_content[:1024],
                    parse_mode=ParseMode.MARKDOWN,
                )

                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            else:
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
            # Retry without markdown and image
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
