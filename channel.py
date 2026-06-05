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
import httpx
from typing import Optional, List, Dict, Tuple
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.types import FSInputFile, ReactionTypeEmoji, InputMediaPhoto
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardButton

from bot.config import config, persona
from bot.database import (
    add_channel_post, get_today_post_count, get_hourly_post_count, get_unposted_news,
    mark_news_posted, add_partner_post, get_today_partner_post_count,
    is_duplicate_post, add_post_fingerprint, cleanup_old_fingerprints,
)
from ai.router import ai_router
from bot.partners import partner_manager
from bot.web_search import web_search, search_news, SearchResult
from bot.interbot import interbot_manager

logger = logging.getLogger("asya.channel")

# ── Reactions to add to posts ───────────────────────────────────────────────

POST_REACTIONS = ["👍", "🔥", "🚗", "😍", "👏", "💯", "🤩", "⚡"]

# ── How many images per news post ───────────────────────────────────────────
# Telegram allows up to 10 media per post. But too many looks spammy.
# We use 1-3 images: real photo from source, or 1-2 AI-generated.
# AI-generated images: only 1 per post (no spam)
NEWS_IMAGES_MIN = 1
NEWS_IMAGES_MAX = 1
# Maximum total images in a channel post (hard limit)
MAX_IMAGES_PER_POST = 3
# Maximum real images to download from RSS (not the Telegram limit!)
MAX_RSS_IMAGES = 2

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

# ── Keyword-based semantic dedup for channel posts ─────────────────────────────
# Stores significant words from recently posted titles
_recent_post_keywords: list = []
_MAX_RECENT_POSTS = 20

# Words to ignore in semantic comparison
_SEMANTIC_STOP_WORDS = frozenset([
    "в", "на", "с", "о", "у", "по", "из", "за", "от", "до", "к", "не", "и", "но",
    "а", "что", "как", "это", "тот", "этот", "для", "при", "через", "между",
    "после", "перед", "без", "под", "над", "об", "со", "то", "же", "ли", "бы",
    "уже", "ещё", "еще", "также", "тоже", "или", "либо", "год", "могут", "будет",
    "стал", "стала", "был", "была", "есть", "может", "очень", "так", "где", "когда",
])


def _is_semantically_duplicate(title: str) -> bool:
    """Check if 3+ significant words from title match a recently posted title."""
    global _recent_post_keywords

    # Extract significant words from the new title
    words = re.findall(r'[a-zа-яё]{3,}', title.lower())
    significant = [w for w in words if w not in _SEMANTIC_STOP_WORDS]

    if len(significant) < 3:
        return False

    for recent_words in _recent_post_keywords:
        matches = sum(1 for w in significant if w in recent_words)
        if matches >= 3:
            return True

    return False


def _record_post_title(title: str):
    """Record a posted title's significant words for semantic dedup."""
    global _recent_post_keywords
    words = re.findall(r'[a-zа-яё]{3,}', title.lower())
    significant = [w for w in words if w not in _SEMANTIC_STOP_WORDS]
    _recent_post_keywords.append(significant)
    if len(_recent_post_keywords) > _MAX_RECENT_POSTS:
        _recent_post_keywords = _recent_post_keywords[-_MAX_RECENT_POSTS:]


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
    """Validate post text before sending to channel.
    
    Checks for:
    - Empty text
    - SSE/API artifacts
    - Blocked topics (politics, war, Putin, etc.)
    - Provider ad artifacts
    - Raw JSON
    """
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

    # Block political/war content — LAST CHANCE filter before posting
    blocked_keywords = [
        "путин", "кремль", "госдума", "президент росс", "президент сша",
        "сво ", "специальная военная", "мобилизац", "санкци",
        "военные действ", "вооруженн", "министр оборон",
        "украин", "нато", "nato",
        "навальн", "оппозиц", "протест", "митинг",
        "политик", "депутат", "законопроект", "выбор ",
    ]
    for keyword in blocked_keywords:
        if keyword in text_lower:
            logger.warning(f"Post BLOCKED (keyword '{keyword}'): {text[:80]}...")
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


def _enforce_char_limit(text: str, has_media: bool) -> str:
    """Smart character limit enforcement — always preserves footer.
    
    Telegram limits:
    - 1024 chars with media (caption)
    - 4096 chars without media (text-only)
    
    This function:
    1. Separates footer from content
    2. Truncates content if needed
    3. Re-attaches footer (always intact)
    """
    footer = "\n\nАвтор @asiaexp_bot\n@sochiautoparts\n#sochiautoparts"
    char_limit = config.TELEGRAM_CAPTION_LIMIT if has_media else config.TELEGRAM_TEXT_LIMIT
    
    if len(text) <= char_limit:
        return text
    
    # Strip existing footer parts to get pure content
    content = text
    for foot_part in ["\n\nАвтор @asiaexp_bot", "\n@sochiautoparts", "\n#sochiautoparts"]:
        content = content.replace(foot_part, "")
    content = content.rstrip()
    
    # Calculate max content length (leave room for footer)
    max_content = char_limit - len(footer)
    if max_content < 100:
        # Absolute minimum — just footer
        return footer.lstrip('\n')
    
    if len(content) > max_content:
        content = content[:max_content - 3] + "..."
    
    return content + footer


class ChannelManager:
    """Manages posting to the @sochiautoparts channel."""

    def __init__(self):
        self._bot: Optional[Bot] = None
        self._last_post_time: float = 0
        self._last_partner_time: float = 0
        self._last_poll_time: float = 0
        self._poll_count: int = 0
        self._post_model_index: int = 0  # For rotating content models

    # Content models rotation — each post uses a different model for variety
    _CONTENT_MODELS_ROTATION = [
        "openai-large", "gpt-5.5", "mistral-4", "deepseek",
        "qwen-large", "deepseek-pro", "deepseek-v4", "minimax-m3",
        "qwen3-coder", "llama-3.3", "nova-2",
    ]

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

    async def _download_news_images(self, image_urls: List[str], max_count: int = 3) -> List[bytes]:
        """Download real images from news source URLs.
        
        Tries each URL, downloads only valid content images.
        Filters out: icons, logos, banners, buttons, social media, tracking pixels,
        and images with abnormal dimensions (too wide/narrow = banners/ads).
        Returns list of image data bytes.
        """
        images = []
        if not image_urls:
            return images

        for url in image_urls[:max_count * 3]:  # Try extra URLs in case some fail
            if len(images) >= max_count:
                break

            # Skip obviously non-content image URLs
            if self._is_junk_image_url(url):
                logger.debug(f"Skipping junk image URL: {url[:80]}")
                continue

            try:
                async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                    response = await client.get(url, headers={
                        "User-Agent": "Mozilla/5.0 (compatible; AsyaBot/1.0; +https://t.me/asiaexp_bot)",
                    })
                    if response.status_code != 200:
                        continue

                    content = response.content
                    content_type = response.headers.get("content-type", "")

                    # Validate: must be an image and at least 20KB (skip tiny icons/pixels/logos/thumbnails)
                    if len(content) < 20000:
                        logger.debug(f"Skipping small image ({len(content)} bytes): {url[:80]}")
                        continue

                    # Skip SVG (vector graphics = logos/icons)
                    if b'<svg' in content[:500] or 'svg' in content_type:
                        continue

                    if not any(ft in content_type for ft in ["image/jpeg", "image/png", "image/webp", "image/gif"]):
                        # Check magic bytes if content-type is missing/wrong
                        if content[:3] == b'\xff\xd8\xff' or content[:4] == b'\x89PNG':
                            pass  # Valid JPEG/PNG
                        elif content[:4] == b'RIFF' and content[8:12] == b'WEBP':
                            pass  # Valid WebP
                        elif content[:6] in (b'GIF87a', b'GIF89a'):
                            pass  # Valid GIF
                        else:
                            continue

                    # Validate image dimensions — skip banners, buttons, tiny thumbnails
                    if not self._is_content_image(content):
                        logger.debug(f"Skipping non-content image: {url[:80]}...")
                        continue

                    images.append(content)
                    logger.info(f"Downloaded news image: {url[:80]}... ({len(content)} bytes)")

            except Exception as e:
                logger.debug(f"Failed to download image {url[:50]}: {e}")
                continue

        logger.info(f"Downloaded {len(images)} real images from news")
        return images

    @staticmethod
    def _is_junk_image_url(url: str) -> bool:
        """Check if an image URL is likely a non-content image (logo, icon, banner, ad, etc.)."""
        url_lower = url.lower()

        # Skip common junk patterns in URL path
        junk_keywords = [
            "icon", "logo", "favicon", "avatar", "badge", "button", "btn",
            "banner", "spinner", "loading", "placeholder", "pixel", "tracker",
            "analytics", "social", "share", "facebook", "twitter", "vk.",
            "telegram", "whatsapp", "instagram", "youtube", "tiktok",
            "ad.", "ads/", "advert", "sponsor", "promo",
            "emoji", "smileys", "captcha", "recaptcha",
            "1x1", "spacer", "blank", "transparent", "dot.", "clear",
            "rss", "feed", "subscribe", "newsletter",
            "watermark", "overlay", "frame", "border",
        ]
        for kw in junk_keywords:
            if kw in url_lower:
                return True

        # Skip URLs with very small size indicators (e.g., 16x16, 32x32, 48x48)
        import re
        size_pattern = re.compile(r'[/=_x](\d{1,3})x(\d{1,3})[/._]')
        size_match = size_pattern.search(url_lower)
        if size_match:
            w, h = int(size_match.group(1)), int(size_match.group(2))
            if w < 100 or h < 100:
                return True  # Too small = icon/thumbnail

        return False

    @staticmethod
    def _is_content_image(image_data: bytes) -> bool:
        """Validate that image data represents a proper content photo, not a banner/ad/logo.
        
        Checks:
        - Minimum size: 20KB (hard filter before this function)
        - Minimum dimensions: 300x200px
        - Maximum aspect ratio: 3:1 (skip wide banners) and 1:3 (skip tall skyscraper ads)
        - Minimum pixel area: 100000px (skip small thumbnails even if file is big)
        """
        # Hard minimum size — nothing under 20KB is a content image
        if len(image_data) < 20000:
            return False

        try:
            from PIL import Image
            import io

            img = Image.open(io.BytesIO(image_data))
            width, height = img.size

            # Skip tiny images (icons, thumbnails)
            if width < 300 or height < 200:
                return False

            # Skip extremely wide images (banners, ad strips)
            if width / max(height, 1) > 3.0:
                return False

            # Skip extremely tall images (skyscraper ads)
            if height / max(width, 1) > 3.0:
                return False

            # Skip very small area images (likely icons/buttons even if > 20KB)
            if width * height < 100000:
                return False

            return True

        except ImportError:
            # PIL not available — REJECT image (safe default: don't post junk)
            logger.warning("PIL not available, REJECTING image (can't validate dimensions)")
            return False
        except Exception:
            # Can't read image — skip it
            return False

    async def _scrape_article_images(self, article_url: str, max_count: int = 2) -> List[bytes]:
        """Scrape images from a news article page as fallback.
        
        Extracts og:image and twitter:image from the article HTML.
        Only uses <img> tags as last resort, with strict filtering.
        Returns list of image data bytes (max 2).
        """
        images = []
        try:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                response = await client.get(article_url, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                })
                if response.status_code != 200:
                    return images

                html = response.text
                
                # Extract og:image first (usually the main article image)
                og_images = re.findall(r'<meta[^>]+property=["\x27]og:image["\x27][^>]+content=["\x27]([^"\x27]+)["\x27]', html, re.IGNORECASE)
                og_images += re.findall(r'<meta[^>]+content=["\x27]([^"\x27]+)["\x27][^>]+property=["\x27]og:image["\x27]', html, re.IGNORECASE)
                
                # Extract twitter:image
                tw_images = re.findall(r'<meta[^>]+name=["\x27]twitter:image["\x27][^>]+content=["\x27]([^"\x27]+)["\x27]', html, re.IGNORECASE)
                tw_images += re.findall(r'<meta[^>]+content=["\x27]([^"\x27]+)["\x27][^>]+name=["\x27]twitter:image["\x27]', html, re.IGNORECASE)
                
                # Extract <img> tags with strict filtering — only from article body areas
                # Look for images inside <article>, <main>, or with class containing 'content'/'article'
                article_html = ""
                for pattern in [r'<article[^>]*>(.*?)</article>', r'<main[^>]*>(.*?)</main>', r'<div[^>]+class=["\x27][^"\x27]*(?:content|article|post|entry)[^"\x27]*["\x27][^>]*>(.*?)</div>']:
                    matches = re.findall(pattern, html, re.IGNORECASE | re.DOTALL)
                    for match in matches:
                        article_html += match + "\n"
                
                # If no article body found, skip <img> tags entirely (too risky)
                all_img_urls = []
                if article_html:
                    all_img_urls = re.findall(r'<img[^>]+src=["\x27]([^"\x27]+)["\x27]', article_html, re.IGNORECASE)
                
                # Prioritize: og:image > twitter:image > article body images
                candidate_urls = []
                seen = set()
                for url_list in [og_images, tw_images, all_img_urls]:
                    for url in url_list:
                        if url and url not in seen and len(url) > 30:
                            if url.startswith("//"):
                                url = "https:" + url
                            # Skip obvious junk even before downloading
                            if self._is_junk_image_url(url):
                                continue
                            seen.add(url)
                            candidate_urls.append(url)
                
                # Download the images (max 2 from scraping)
                images = await self._download_news_images(candidate_urls, max_count=max_count)

        except Exception as e:
            logger.debug(f"Article scraping failed for {article_url[:50]}: {e}")

        return images

    async def _generate_post_images(self, news_title: str, count: int = 1) -> List[bytes]:
        """Generate multiple images for a news post using AI (fallback).
        Returns list of image data bytes, up to `count` images.
        """
        images = []
        # Different prompts for variety
        prompts = [
            f"Automotive news illustration: {news_title}. Professional automotive photography, "
            f"front three-quarter view, modern car, vibrant colors, high quality, dramatic lighting, no text.",
            f"Automotive news illustration: {news_title}. Side profile shot, "
            f"studio lighting, sleek design, magazine quality, no text overlay.",
            f"Automotive news illustration: {news_title}. Rear angle view, "
            f"dynamic composition, professional car photography, vivid colors, no text.",
            f"Automotive news illustration: {news_title}. Interior detail shot, "
            f"dashboard and steering wheel, premium feel, cinematic lighting, no text.",
            f"Automotive news illustration: {news_title}. Detail close-up, "
            f"headlight or wheel, dramatic lighting, high contrast, no text.",
        ]
        selected_prompts = prompts[:min(count, len(prompts))]

        for i, prompt in enumerate(selected_prompts):
            try:
                image_data = await ai_router._primary.generate_image(prompt, model="flux")
                if image_data:
                    images.append(image_data)
            except Exception as e:
                logger.error(f"Image generation #{i+1} failed: {e}")

        logger.info(f"Generated {len(images)}/{count} AI images for post")
        return images

    async def _generate_post_image(self, news_title: str) -> Optional[bytes]:
        """Generate a single image for a news post using AI (backward compat)."""
        images = await self._generate_post_images(news_title, count=1)
        return images[0] if images else None

    async def _get_post_images(self, news_item: Dict) -> tuple:
        """Get images for a news post with smart strategy.
        
        Strategy:
        1. Try real images from RSS feed (image_urls field)
        2. If not enough, try scraping article page for images
        3. If still no images, generate AI images as fallback
        
        Returns (image_list: List[bytes], source: str)
        source is 'real', 'scraped', or 'ai' for logging.
        """
        image_list = []
        source = "none"
        
        # Strategy 1: Use real images from RSS feed
        rss_image_urls = news_item.get("image_urls", [])
        if rss_image_urls:
            try:
                image_list = await self._download_news_images(
                    rss_image_urls, 
                    max_count=MAX_RSS_IMAGES  # Only 2 real images max from RSS
                )
                if image_list:
                    source = "real"
                    logger.info(f"Using {len(image_list)} real images from RSS for: {news_item.get('title', '')[:50]}")
            except Exception as e:
                logger.warning(f"Failed to download RSS images: {e}")
        
        # Strategy 2: Scrape article page for images (only if no real images from RSS)
        if not image_list and news_item.get("url"):
            try:
                scraped = await self._scrape_article_images(
                    news_item["url"], 
                    max_count=2
                )
                if scraped:
                    image_list.extend(scraped)
                    source = "scraped" if source == "none" else source + "+scraped"
                    logger.info(f"Scraped {len(scraped)} additional images for: {news_item.get('title', '')[:50]}")
            except Exception as e:
                logger.debug(f"Article scraping skipped: {e}")
        
        # Strategy 3: AI generation as fallback (1 image only — no spam!)
        if not image_list:
            try:
                image_list = await self._generate_post_images(
                    news_item.get("title", ""), count=1  # Always 1 AI image
                )
                if image_list:
                    source = "ai"
                    logger.info(f"Generated {len(image_list)} AI images (no real images found)")
            except Exception as e:
                logger.warning(f"AI image generation skipped: {e}")
        
        # HARD LIMIT: never more than MAX_IMAGES_PER_POST (3 max — no spam!)
        image_list = image_list[:MAX_IMAGES_PER_POST]
        
        if len(image_list) > MAX_IMAGES_PER_POST:
            logger.warning(f"Image limit exceeded! Had {len(image_list)}, capping to {MAX_IMAGES_PER_POST}")
        
        return image_list, source

    async def _download_partner_image(self, image_url: str) -> Optional[bytes]:
        """Download a partner program image (logo/banner).
        Applies same strict validation as news images to avoid posting junk."""
        if not image_url:
            return None
        try:
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                response = await client.get(image_url)
                if response.status_code != 200:
                    return None
                content = response.content
                content_type = response.headers.get("content-type", "")

                # Strict size check — nothing under 20KB
                if len(content) < 20000:
                    logger.debug(f"Skipping small partner image ({len(content)} bytes)")
                    return None

                # Must be an image type
                if not any(ft in content_type for ft in ["image/png", "image/jpeg", "image/gif", "image/webp"]):
                    # Check magic bytes
                    if not (content[:3] == b'\xff\xd8\xff' or content[:4] == b'\x89PNG' or
                            (content[:4] == b'RIFF' and content[8:12] == b'WEBP')):
                        logger.debug(f"Skipping partner image: not an image, content-type={content_type}")
                        return None

                # Validate dimensions (same as news images)
                if not self._is_content_image(content):
                    logger.debug(f"Skipping non-content partner image")
                    return None

                return content
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

        # Check hourly limit — max 2 posts per hour
        hourly_count = await get_hourly_post_count()
        if hourly_count >= config.CHANNEL_MAX_POSTS_PER_HOUR:
            logger.info(f"Hourly post limit reached ({hourly_count}/{config.CHANNEL_MAX_POSTS_PER_HOUR})")
            return False

        # Check minimum interval
        min_interval = config.CHANNEL_POST_INTERVAL_MINUTES * 60
        if time.time() - self._last_post_time < min_interval:
            logger.info("Post interval too short")
            return False

        # Get news item if not provided
        if not news_item:
            unposted = await get_unposted_news(limit=10)
            if unposted:
                # Filter out duplicates using fingerprint system
                for item in unposted:
                    if await is_duplicate_post(item.get("title", ""), hours=72):
                        logger.info(f"Skipping duplicate news: {item.get('title', '')[:60]}")
                        # Mark as posted so we don't pick it again
                        if item.get("url"):
                            await mark_news_posted(item["url"])
                        continue
                    auto_news = [n for n in [item] if n.get("category") == "auto"]
                    if auto_news:
                        news_item = auto_news[0]
                    else:
                        news_item = item
                    break
                
                # If all were duplicates, try internet search
                if not news_item:
                    news_item = await self._search_internet_news()
                    if not news_item:
                        logger.info("All unposted news are duplicates, and no internet news found")
                        return False
            else:
                news_item = await self._search_internet_news()
                if not news_item:
                    logger.info("No unposted news available (RSS or internet)")
                    return False

        # ── DEDUPLICATION: Check if this news was already posted ──
        if news_item and news_item.get("title"):
            if await is_duplicate_post(news_item["title"], hours=72):
                logger.warning(f"DUPLICATE post blocked: {news_item['title'][:60]}")
                # Mark as posted to avoid picking again
                if news_item.get("url"):
                    await mark_news_posted(news_item["url"])
                return False

            # Keyword-based semantic dedup: if 3+ significant words match a recently posted title, skip
            if _is_semantically_duplicate(news_item["title"]):
                logger.warning(f"SEMANTIC DUPLICATE blocked: {news_item['title'][:60]}")
                if news_item.get("url"):
                    await mark_news_posted(news_item["url"])
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

        # Get images: real from news source → scraped from article → AI generated
        image_list: List[bytes] = []
        image_source = "none"
        has_media = False
        try:
            image_list, image_source = await self._get_post_images(news_item)
            has_media = len(image_list) > 0
        except Exception as e:
            logger.warning(f"Image retrieval skipped: {e}")

        media_count = len(image_list) if has_media else 0
        
        # Tell AI whether images are real or generated so it can adjust tone
        if image_source in ("real", "scraped", "real+scraped"):
            extra_instructions += (
                "К посту прикреплены РЕАЛЬНЫЕ фотографии из новости. "
                "Не описывай фото — они уже прикреплены. Пиши текст новости. "
            )
        elif has_media:
            extra_instructions += (
                "К посту прикреплены сгенерированные иллюстрации. "
                "Не описывай их подробно — они иллюстративные. "
            )

        response = await ai_router.generate_channel_post(
            topic=news_item["title"],
            source_text=source_text,
            extra_instructions=extra_instructions,
            has_media=has_media,
            media_count=media_count,
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

        # Smart character limit enforcement — always preserve footer
        post_text = _enforce_char_limit(post_text, has_media)

        # ── AI-Filter: Check with Настя before publishing ──
        try:
            candidate_id = await interbot_manager.submit_news_candidate(
                title=news_item.get("title", ""),
                summary=news_item.get("summary", "")[:300],
                category=news_item.get("category", "auto"),
            )

            # Wait briefly for review (max 10 seconds)
            review_found = False
            for _ in range(5):
                await asyncio.sleep(2)
                reviewed = await interbot_manager.get_reviewed_candidates()
                for item in reviewed:
                    if item["candidate"].get("id") == candidate_id:
                        review = item["review"]
                        verdict = review.get("verdict", "approved")
                        if verdict == "rejected":
                            logger.info(f"Настя REJECTED post: {news_item.get('title', '')[:50]}")
                            await interbot_manager.mark_candidate_processed(candidate_id)
                            return False
                        elif verdict == "improved" and review.get("improved_text"):
                            post_text = _clean_post_text(review["improved_text"])
                            post_text = _ensure_footer(post_text)
                            post_text = _enforce_char_limit(post_text, has_media)
                            logger.info(f"Настя IMPROVED post: {news_item.get('title', '')[:50]}")
                        else:
                            logger.info(f"Настя APPROVED post: {news_item.get('title', '')[:50]}")
                        await interbot_manager.mark_candidate_processed(candidate_id)
                        review_found = True
                        break
                if review_found:
                    break
            else:
                # No review from Настя — publish independently (fallback)
                logger.info(f"No review from Настя, publishing independently")
        except Exception as e:
            logger.warning(f"AI-Filter check failed, publishing without review: {e}")

        # Re-validate after potential modification by Настя
        if not _validate_post_text(post_text):
            logger.error(f"Post validation failed after review, skipping")
            return False

        # HARD SAFETY CHECK: never post more than MAX_IMAGES_PER_POST images
        if has_media and image_list and len(image_list) > MAX_IMAGES_PER_POST:
            logger.warning(f"SAFETY: Truncating {len(image_list)} images to {MAX_IMAGES_PER_POST}")
            image_list = image_list[:MAX_IMAGES_PER_POST]

        # Post to channel
        try:
            if has_media and image_list:
                # Save images to temp files
                tmp_paths = []
                for i, img_data in enumerate(image_list[:MAX_IMAGES_PER_POST]):
                    tmp_path = os.path.join(tempfile.gettempdir(), f"asya_post_{int(time.time())}_{i}.png")
                    with open(tmp_path, "wb") as f:
                        f.write(img_data)
                    tmp_paths.append(tmp_path)

                if len(tmp_paths) == 1:
                    # Single image — use send_photo
                    photo = FSInputFile(tmp_paths[0], filename="asya_post.png")
                    sent = await self._bot.send_photo(
                        chat_id=config.CHANNEL_ID,
                        photo=photo,
                        caption=post_text[:config.TELEGRAM_CAPTION_LIMIT],
                        parse_mode=ParseMode.HTML,
                        disable_notification=True,
                    )
                else:
                    # Multiple images — use send_media_group (album/carousel up to 10)
                    media_group = []
                    for i, tmp_path in enumerate(tmp_paths):
                        photo_file = FSInputFile(tmp_path, filename=f"asya_post_{i}.png")
                        if i == 0:
                            # First image gets the caption
                            media_group.append(InputMediaPhoto(
                                media=photo_file,
                                caption=post_text[:config.TELEGRAM_CAPTION_LIMIT],
                                parse_mode=ParseMode.HTML,
                            ))
                        else:
                            media_group.append(InputMediaPhoto(media=photo_file))

                    messages = await self._bot.send_media_group(
                        chat_id=config.CHANNEL_ID,
                        media=media_group,
                        disable_notification=True,
                    )
                    sent = messages[0]  # First message in group for DB tracking

                # Clean up temp files
                for tmp_path in tmp_paths:
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

            # ── DEDUPLICATION: Store fingerprint to prevent duplicates ──
            await add_post_fingerprint(
                title=news_item.get("title", ""),
                content=post_text,
                post_id=sent.message_id,
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

            # Record title for semantic dedup
            _record_post_title(news_item.get("title", ""))

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

                # ── DEDUPLICATION: Store fingerprint for fallback post too ──
                await add_post_fingerprint(
                    title=news_item.get("title", ""),
                    content=post_text,
                    post_id=sent.message_id,
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
        """Search the internet for fresh auto news when RSS feeds are empty.
        Uses both web search and Perplexity AI for better coverage."""
        try:
            now = datetime.now(_MOSCOW_TZ)
            queries = [
                f"автомобильные новости сегодня {now.year}",
                "новости автопрома новинки авто",
                "auto news latest today",
            ]
            query = random.choice(queries)

            # Strategy 1: Web search
            results = await web_search(query, max_results=5)

            if results:
                random.shuffle(results)
                for result in results:
                    if not result.title:
                        continue

                    news_item = {
                        "title": result.title,
                        "url": result.url,  # Unique URL from search result
                        "summary": result.snippet or "",
                        "category": "auto",
                        "lang": "ru" if any(c >= '\u0400' for c in result.title) else "en",
                        "image_urls": [],  # Will be filled by scraping if available
                    }

                    # Check for duplicate BEFORE returning
                    if await is_duplicate_post(result.title, hours=72):
                        logger.info(f"Internet news is duplicate: {result.title[:60]}")
                        continue

                    logger.info(f"Found internet news: {news_item['title'][:50]}")
                    return news_item

            # Strategy 2: Perplexity AI search (web-augmented)
            try:
                response = await ai_router._primary.chat(
                    messages=[
                        {"role": "system", "content": (
                            "Ты поисковик автоновостей. Найди самую свежую и интересную "
                            "автомобильную новость за сегодня. Верни ТОЛЬКО название новости "
                            "и краткое описание (2-3 предложения). Формат:\n"
                            "ЗАГОЛОВОК: ...\nОПИСАНИЕ: ..."
                        )},
                        {"role": "user", "content": f"Найди свежую автоновость ({now.strftime('%d.%m.%Y')})"},
                    ],
                    model="perplexity-fast",
                    temperature=0.5,
                    max_tokens=300,
                )

                if not response.error and response.text:
                    text = response.text.strip()
                    title = ""
                    summary = ""

                    if "ЗАГОЛОВОК:" in text:
                        parts = text.split("ОПИСАНИЕ:")
                        title = parts[0].replace("ЗАГОЛОВОК:", "").strip()
                        summary = parts[1].strip() if len(parts) > 1 else ""
                    else:
                        # Fallback: use first line as title
                        lines = text.split('\n', 1)
                        title = lines[0].strip()
                        summary = lines[1].strip() if len(lines) > 1 else text

                    if title:
                        return_dict = {
                            "title": title,
                            "url": f"https://t.me/sochiautoparts/perplexity/{int(time.time())}",  # Unique URL
                            "summary": summary,
                            "category": "auto",
                            "lang": "ru",
                        }
                        # Check for duplicate before returning
                        if not await is_duplicate_post(title, hours=48):
                            return return_dict
                        else:
                            logger.info(f"Perplexity news is duplicate: {title[:60]}")
            except Exception as e:
                logger.debug(f"Perplexity news search failed: {e}")

            return None

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

        # Try to use partner image FIRST (before generating text, so we know has_media)
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

        post_content = await partner_manager.generate_partner_post_content(program)

        response = await ai_router.generate_channel_post(
            topic=f"Рекомендация сервиса: {program.name}",
            source_text=f"Партнёрская программа: {program.name}. Описание: {program.description or 'Автомобильный сервис'}",
            extra_instructions=(
                "Это партнёрский пост — рекомендация сервиса для автомобилистов. "
                "Напиши естественно, как живая девушка-автоэксперт, которая советует проверенный сервис. "
                "Не делай это откровенной рекламой — вставь рекомендацию органично. "
                f"Ссылка: {program.format_link()}"
            ),
            has_media=has_media,
            media_count=1,
        )

        if not response.error and response.text:
            post_content = response.text

        post_content = _clean_post_text(post_content)
        post_content = _ensure_footer(post_content)

        # Validate
        if not _validate_post_text(post_content):
            return False

        # Smart character limit — always preserve footer
        post_content = _enforce_char_limit(post_content, has_media)

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
