"""
Channel Manager — Posts to @sochiautoparts with proper formatting.
Handles news posts, partner posts, scheduled content, reactions,
media, polls, and internet news search.
Properly enforces Telegram character limits: 1024 with media, 4096 without.
Posts silently (disable_notification) matching channel settings.
"""

import logging
import time
import random
import asyncio
import tempfile
import os
import re
from typing import Optional, List, Dict
from datetime import datetime
from zoneinfo import ZoneInfo

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

# ── Poll topics for channel engagement ──────────────────────────────────────

POLL_TEMPLATES = [
    "Что думаете об этой новости?",
    "Ваше мнение?",
    "Как вам такая новость?",
    "Оцените новость!",
    "Что скажете?",
]

# Moscow timezone
_MOSCOW_TZ = ZoneInfo("Europe/Moscow")


def _clean_post_text(text: str) -> str:
    """Clean post text: remove markdown links, formatting artifacts, SSE garbage."""
    if not text:
        return text

    # Remove markdown links [text](url) → text (Telegram doesn't support them properly)
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'\1', text)

    # Remove bold/italic
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)

    # Remove SSE/streaming artifacts
    text = re.sub(r'data:\s*\{[^}]*\}', '', text)
    text = re.sub(r'\[DONE\]', '', text)

    # Remove AI disclaimers
    for phrase in ["As an AI", "Как AI", "Как искусственный интеллект",
                   "powered by pollinations", "pollinations.ai"]:
        text = re.sub(rf'.*{re.escape(phrase)}.*', '', text, flags=re.IGNORECASE)

    # Remove think tags
    text = re.sub(r'<think\b[^>]*>.*?</think\s*>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'</?think[^>]*>', '', text, flags=re.IGNORECASE)

    # Remove "Настя:" or "Ася:" prefixes
    for prefix in ["Настя:", "Ася:", "Nastya:", "Asya:", "Assistant:"]:
        if text.startswith(prefix):
            text = text[len(prefix):].strip()

    # Clean up whitespace
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = text.strip()

    return text


def _validate_post_text(text: str) -> bool:
    """Validate post text before sending to channel."""
    if not text or not text.strip():
        return False

    text_lower = text.lower()

    # Block SSE artifacts
    sse_patterns = [r'data:\s*\{', r'\[DONE\]', r'"type"\s*:\s*"start"', r'"type"\s*:\s*"error"']
    for pattern in sse_patterns:
        if re.search(pattern, text_lower):
            return False

    # Block API errors
    error_patterns = ["authentication error", "no api key", "model not found",
                      "rate limit", "internal server error", "bad request"]
    for pattern in error_patterns:
        if pattern in text_lower:
            return False

    # Block provider ad artifacts
    ad_patterns = ["pollinations.ai", "powered by pollinations", "keep ai accessible"]
    for pattern in ad_patterns:
        if pattern in text_lower:
            return False

    # Block raw JSON
    if text.strip().startswith(('{', '[', '```', 'data:')):
        return False

    return True


def _ensure_footer(text: str) -> str:
    """Ensure post has proper footer matching @sochiautoparts format."""
    # Remove any existing footer elements to avoid duplicates
    text = re.sub(r'\n*Автор\s+@asiaexp_bot', '', text)
    text = re.sub(r'\n*@sochiautoparts', '', text)
    text = re.sub(r'\n*#sochiautoparts', '', text)
    text = text.rstrip()
    # Add proper footer: Author link + channel mention + hashtag
    text += "\n\nАвтор @asiaexp_bot\n@sochiautoparts\n#sochiautoparts"
    return text


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
                    content_type = response.headers.get("content-type", "")
                    if any(ft in content_type for ft in ["image/png", "image/jpeg", "image/gif", "image/webp"]):
                        return response.content
                    elif len(response.content) > 5000:
                        return response.content
                    else:
                        logger.debug(f"Skipping partner image: content-type={content_type}, size={len(response.content)}")
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

            import json
            text = response.text.strip()
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
                auto_news = [n for n in unposted if n.get("category") == "auto"]
                if auto_news:
                    news_item = random.choice(auto_news)
                else:
                    news_item = unposted[0]
            else:
                news_item = await self._search_internet_news()
                if not news_item:
                    logger.info("No unposted news available (RSS or internet)")
                    return False

        # Generate post content using AI
        source_text = ""
        if news_item.get("summary"):
            source_text = news_item["summary"]

        extra_instructions = (
            "Уникализируй текст — перепиши своими словами, сохранив факты. "
            "Не копируй оригинальные формулировки. "
            "Добавь своё мнение и эмоции как живой девушки-автоэксперта. "
        )
        if news_item.get("lang") and news_item.get("lang") != "ru":
            extra_instructions += (
                "Это новость из зарубогного источника. "
                "Переведи на русский язык и адаптируй для русскоязычной аудитории. "
                "Сохрани суть и факты, но напиши естественно на русском."
            )

        # Try to generate image
        has_media = False
        image_data = None
        try:
            image_data = await self._generate_post_image(news_item["title"])
            has_media = image_data is not None
        except Exception as e:
            logger.warning(f"Image generation skipped: {e}")

        response = await ai_router.generate_channel_post(
            topic=news_item["title"],
            source_text=source_text,
            extra_instructions=extra_instructions,
            has_media=has_media,
        )

        if response.error or not response.text:
            logger.error(f"Failed to generate channel post: {response.error_message}")
            return False

        post_text = _clean_post_text(response.text)
        post_text = _ensure_footer(post_text)

        # Validate before posting
        if not _validate_post_text(post_text):
            logger.error(f"Post validation failed, skipping")
            return False

        # Enforce character limits
        char_limit = config.TELEGRAM_CAPTION_LIMIT if has_media else config.TELEGRAM_TEXT_LIMIT
        if len(post_text) > char_limit:
            post_text = post_text[:char_limit - 3] + "..."

        # Post to channel
        try:
            if has_media and image_data:
                tmp_path = os.path.join(tempfile.gettempdir(), f"asya_post_{int(time.time())}.png")
                with open(tmp_path, "wb") as f:
                    f.write(image_data)

                photo = FSInputFile(tmp_path, filename="asya_post.png")
                sent = await self._bot.send_photo(
                    chat_id=config.CHANNEL_ID,
                    photo=photo,
                    caption=post_text[:config.TELEGRAM_CAPTION_LIMIT],
                    parse_mode=ParseMode.HTML,
                    disable_notification=True,
                )

                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            else:
                sent = await self._bot.send_message(
                    chat_id=config.CHANNEL_ID,
                    text=post_text,
                    parse_mode=ParseMode.HTML,
                    disable_notification=True,
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

            # Occasionally create a poll based on the news (every 3rd post)
            self._poll_count += 1
            if self._poll_count % 3 == 0:
                asyncio.create_task(self._post_poll(news_item))

            logger.info(f"Posted news to channel: {news_item['title'][:50]}...")
            return True

        except Exception as e:
            logger.error(f"Error posting to channel: {e}")
            # Try sending without formatting
            try:
                sent = await self._bot.send_message(
                    chat_id=config.CHANNEL_ID,
                    text=post_text,
                    disable_notification=True,
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
                return True
            except Exception as e2:
                logger.error(f"Error posting without formatting: {e2}")
                return False

    async def _search_internet_news(self) -> Optional[Dict]:
        """Search the internet for fresh auto news when RSS feeds are empty."""
        try:
            now = datetime.now(_MOSCOW_TZ)
            queries = [
                f"автомобильные новости сегодня {now.year}",
                "новости автопрома новинки авто",
                "auto news latest today",
            ]
            query = random.choice(queries)
            results = await web_search(query, max_results=5)

            if not results:
                return None

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
            now = time.time()
            if now - self._last_poll_time < 3600:
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
                disable_notification=True,
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

        if not partner_manager.should_post_partner():
            return False

        today_count = await get_today_post_count()
        if today_count >= config.CHANNEL_MAX_POSTS_PER_DAY:
            return False

        program = partner_manager.get_random_program()
        if not program:
            logger.info("No partner programs available")
            return False

        post_content = await partner_manager.generate_partner_post_content(program)

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

        post_content = _clean_post_text(post_content)
        post_content = _ensure_footer(post_content)

        # Validate
        if not _validate_post_text(post_content):
            return False

        # Try to use partner image
        partner_image_url = program.image if hasattr(program, 'image') else None
        partner_image_data = None
        if partner_image_url:
            try:
                partner_image_data = await self._download_partner_image(partner_image_url)
            except Exception as e:
                logger.debug(f"Partner image download failed: {e}")

        if not partner_image_data:
            try:
                partner_image_data = await self._generate_post_image(f"{program.name} automotive service")
            except Exception:
                pass

        has_media = partner_image_data is not None
        char_limit = config.TELEGRAM_CAPTION_LIMIT if has_media else config.TELEGRAM_TEXT_LIMIT
        if len(post_content) > char_limit:
            post_content = post_content[:char_limit - 3] + "..."

        try:
            if has_media and partner_image_data:
                ext = ".png"
                if partner_image_url and ".jpg" in partner_image_url.lower():
                    ext = ".jpg"

                tmp_path = os.path.join(tempfile.gettempdir(), f"asya_partner_{int(time.time())}{ext}")
                with open(tmp_path, "wb") as f:
                    f.write(partner_image_data)

                photo = FSInputFile(tmp_path, filename=f"partner_{program.name[:20]}.{ext.lstrip('.')}")
                sent = await self._bot.send_photo(
                    chat_id=config.CHANNEL_ID,
                    photo=photo,
                    caption=post_content[:config.TELEGRAM_CAPTION_LIMIT],
                    parse_mode=ParseMode.HTML,
                    disable_notification=True,
                )

                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            else:
                sent = await self._bot.send_message(
                    chat_id=config.CHANNEL_ID,
                    text=post_content,
                    parse_mode=ParseMode.HTML,
                    disable_notification=True,
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

            logger.info(f"Posted partner content: {program.name}")
            return True

        except Exception as e:
            logger.error(f"Error posting partner content: {e}")
            try:
                sent = await self._bot.send_message(
                    chat_id=config.CHANNEL_ID,
                    text=post_content,
                    disable_notification=True,
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
                logger.error(f"Error posting partner content without formatting: {e2}")
                return False

    async def run_scheduled_post(self) -> bool:
        """
        Run a scheduled post — either news or partner content.
        """
        now = time.time()
        partner_interval = config.PARTNER_POST_INTERVAL_HOURS * 3600

        if (now - self._last_partner_time >= partner_interval and
                partner_manager.should_post_partner()):
            if random.random() < 0.3:
                return await self.post_partner_content()

        return await self.post_news()


# ── Global instance ────────────────────────────────────────────────────────────

channel_manager = ChannelManager()
