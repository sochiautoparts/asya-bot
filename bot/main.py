"""Ася Main — starts OpenClaw gateway + aiogram bot + auto channel scheduler."""
import asyncio, logging, os, signal, subprocess, sys, time, random
from pathlib import Path
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from bot.config import config
from bot import database as db
from bot.mood import mood_loop, current_mood_descriptor
from bot.partners import partner_manager
from ai import client as ai_client
from bot.post_utils import (
    smart_truncate, clean_post_text, validate_post_text,
    needs_translation, validate_image, title_fingerprint,
    text_fingerprint, url_normalize, date_context, UNIQUIFICATION_RULES,
)

logging.basicConfig(level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO), format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger("asya.main")
for noisy in ["aiogram.event", "httpx", "httpcore", "aiosqlite"]: logging.getLogger(noisy).setLevel(logging.WARNING)

from bot.handlers.chat import chat_router
from bot.handlers.groups import group_router
from bot.handlers.channels import channel_router
from bot.handlers.admin import admin_router
from bot.handlers.inline import inline_router

OPENCLAW_STATE_DIR = os.getenv("OPENCLAW_STATE_DIR", str(Path.cwd() / ".openclaw-state"))
_openclaw_proc = None

def _generate_openclaw_config():
    state_dir = OPENCLAW_STATE_DIR
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    out = str(Path(state_dir) / "openclaw.json")
    gen = str(Path(__file__).resolve().parent.parent / "scripts" / "gen_openclaw_config.py")
    env = os.environ.copy(); env["OPENCLAW_STATE_DIR"] = state_dir
    r = subprocess.run([sys.executable, gen, "--out", out, "--state-dir", state_dir], env=env)
    if r.returncode != 0: raise RuntimeError(f"OpenClaw config generation failed (code {r.returncode})")
    return out

def _start_openclaw_gateway(config_path):
    env = os.environ.copy()
    env["OPENCLAW_STATE_DIR"] = OPENCLAW_STATE_DIR
    env["OPENCLAW_CONFIG_PATH"] = config_path
    npm_global = os.path.expanduser("~/.npm-global/bin")
    env["PATH"] = npm_global + ":" + env.get("PATH", "")
    cmd = [config.OPENCLAW_BIN, "gateway", "--port", str(config.OPENCLAW_PORT), "--auth", "none", "--bind", "loopback", "--allow-unconfigured"]
    log_path = str(Path(OPENCLAW_STATE_DIR) / "gateway.log")
    logger.info(f"Starting OpenClaw Gateway: {' '.join(cmd)}")
    log_f = open(log_path, "a", buffering=1)
    return subprocess.Popen(cmd, env=env, stdout=log_f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)

async def _wait_for_gateway(timeout=120.0):
    import httpx
    url = f"{config.OPENCLAW_URL}/v1/models"
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            async with httpx.AsyncClient() as c:
                r = await c.get(url, timeout=5.0)
                if r.status_code == 200: return True
        except: pass
        if _openclaw_proc is not None and _openclaw_proc.poll() is not None: return False
        await asyncio.sleep(2.0)
    return False

def _stop_openclaw_gateway():
    global _openclaw_proc
    if _openclaw_proc is not None:
        try:
            _openclaw_proc.terminate()
            try: _openclaw_proc.wait(timeout=10)
            except: _openclaw_proc.kill()
        except: pass
        _openclaw_proc = None

class AsyaBot:
    def __init__(self):
        if not config.BOT_TOKEN: raise RuntimeError("BOT_TOKEN not set")
        self.bot = Bot(token=config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=None))
        self.dp = Dispatcher(storage=MemoryStorage())
        self.dp.include_router(admin_router)
        self.dp.include_router(chat_router)
        self.dp.include_router(group_router)
        self.dp.include_router(channel_router)
        self.dp.include_router(inline_router)
        from aiogram.types import ErrorEvent
        @self.dp.error()
        async def on_error(event: ErrorEvent):
            try:
                exc = event.exception
                from aiogram.exceptions import TelegramRetryAfter
                if isinstance(exc, TelegramRetryAfter): logger.warning(f"Flood control (RetryAfter {exc.retry_after}s)")
                else: logger.error(f"Handler error (suppressed): {type(exc).__name__}: {exc}", exc_info=False)
            except: pass

    async def start(self):
        logger.info("=== Ася (OpenClaw) стартует ===")
        try:
            me = await self.bot.get_me()
            config.BOT_ID = me.id
            config.BOT_USERNAME = (me.username or config.BOT_USERNAME or "").lstrip("@")
            logger.info(f"Bot: @{config.BOT_USERNAME} (id={config.BOT_ID}) «{me.first_name or ''}», owner={config.OWNER_ID}")
        except Exception as e: logger.warning(f"get_me failed: {e}")
        await db.init_db()
        logger.info("DB initialized")
        # Load posted_news from file backup (prevents duplicates after restart)
        try:
            await db.load_posted_news_from_file()
        except Exception as e:
            logger.warning(f"load_posted_news_from_file failed: {e}")
        try:
            await partner_manager.load()
            logger.info(f"Partners loaded: {len(partner_manager.campaigns)} campaigns")
        except: pass
        await ai_client.initialize()
        logger.info(f"AI client ready — {config.providers_status()}")
        asyncio.create_task(mood_loop(), name="mood_loop")
        asyncio.create_task(db.run_periodic_cleanup(), name="cleanup_loop")
        try:
            from bot.proactive import proactive_loop, summary_loop, set_bot
            set_bot(self.bot)
            asyncio.create_task(proactive_loop(), name="proactive_loop")
            asyncio.create_task(summary_loop(), name="summary_loop")
            logger.info("Proactive + summary loops enabled")
        except Exception as e: logger.warning(f"Proactive failed: {e}")
        # Auto Channel scheduler — Ася posts 2 news to @sochiautoparts every 20 min
        if config.CHANNEL_ID:
            asyncio.create_task(self._channel_scheduler(), name="channel_scheduler")
            # Partner (affiliate) scheduler — 1 promo post per hour
            asyncio.create_task(self._partner_scheduler(), name="partner_scheduler")
            logger.info(f"Channel scheduler enabled (@{config.CHANNEL_USERNAME}) — 2 news/20min + 1 partner/hour")
        await self._notify_owner()
        try: await self.bot.delete_webhook(drop_pending_updates=True)
        except: pass
        allowed = ["message", "edited_message", "channel_post", "edited_channel_post", "inline_query", "chosen_inline_result"]
        logger.info("=== Ася в сети — слушаю сообщения ===")
        polling_retries = 0
        while True:
            try:
                await self.dp.start_polling(self.bot, allowed_updates=allowed)
                break
            except Exception as e:
                polling_retries += 1
                logger.error(f"Polling error (attempt {polling_retries}): {type(e).__name__}: {e}")
                if polling_retries > 50: break
                await asyncio.sleep(5 if polling_retries <= 5 else 10)
        try: await ai_client.close()
        except: pass

    async def _channel_scheduler(self):
        """Background task: post 2 auto-news to @sochiautoparts every 20 min.
        Editorial voice (от имени редакции). Posts up to 2 unposted items per cycle,
        with a short gap between them.
        """
        from bot.persona import CHANNEL_POST_PROMPT
        from bot.post_utils import topic_fingerprint
        await asyncio.sleep(120)
        post_interval = 1200  # 20 min — 2 posts per cycle
        NEWS_URL = "https://raw.githubusercontent.com/sochiautoparts/nws/main/data/auto-news.json"

        while True:
            try:
                channel_id = int(config.CHANNEL_ID)
                mood = await current_mood_descriptor()

                # 1. Fetch auto-news.json
                import httpx
                async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                    resp = await client.get(NEWS_URL, headers={"User-Agent": "AsyaBot/1.0"})
                if resp.status_code != 200:
                    logger.warning(f"News fetch failed: HTTP {resp.status_code}")
                    await asyncio.sleep(post_interval)
                    continue

                news_data = resp.json()
                all_items = news_data.get("items", [])
                if not all_items:
                    logger.warning("No news items in auto-news.json")
                    await asyncio.sleep(post_interval)
                    continue

                logger.info(f"Fetched {len(all_items)} news items from auto-news.json")

                # 2. Find up to 8 candidate news items (to retry if AI empty/validation fail)
                # Dedup by news_id AND URL AND title fingerprint AND topic fingerprint
                candidates = []
                seen_titles = set()
                for item in all_items:
                    news_id = item.get("id", "")
                    title = item.get("title", "")
                    item_url = item.get("url", "")
                    if news_id and await db.is_news_posted(news_id):
                        continue
                    if item_url and await db.is_news_posted(url_normalize(item_url)):
                        continue
                    tf = title_fingerprint(title)
                    if tf and await db.is_news_posted(f"tf:{tf}"):
                        continue
                    topic = topic_fingerprint(title, item.get("summary", ""))
                    if topic and len(topic.split()) >= 2 and await db.is_news_posted(f"topic:{topic}"):
                        continue
                    if tf and tf in seen_titles:
                        continue
                    seen_titles.add(tf)
                    candidates.append(item)
                    if len(candidates) >= 8:
                        break

                if not candidates:
                    logger.info("All news items already posted — picking random for AI uniquification")
                    import random as _rng
                    candidates = _rng.sample(all_items, min(4, len(all_items)))

                # 3. Try candidates until we post 2 (or exhaust candidates)
                posted_count = 0
                target_posts = 2
                for news_item in candidates:
                    if posted_count >= target_posts:
                        break
                    try:
                        posted = await self._post_news_item(news_item, mood, channel_id, CHANNEL_POST_PROMPT)
                        if posted:
                            posted_count += 1
                            logger.info(f"Cycle: posted news {posted_count}/{target_posts}")
                            if posted_count < target_posts:
                                await asyncio.sleep(5)  # gap between posts (was 60s)
                        else:
                            logger.info(f"News skipped (AI empty or validation) — trying next candidate")
                    except Exception as e:
                        logger.error(f"Post news item error: {e}")
                logger.info(f"Cycle complete: posted {posted_count}/{target_posts} from {len(candidates)} candidates")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Channel scheduler error: {e}")

            await asyncio.sleep(post_interval)

    async def _post_news_item(self, news_item, mood, channel_id, channel_prompt):
        """Post a single news item to channel with photo (editorial voice). Returns True if posted.

        Full pipeline: translate (if EN) → AI generate → clean → validate → smart truncate → post.
        Handles: 1 photo → send_photo; 2+ photos → send_media_group; 0 → send_message.
        """
        import httpx
        from bot.post_utils import (smart_truncate, clean_post_text, validate_post_text,
            needs_translation, validate_image, title_fingerprint, text_fingerprint,
            url_normalize, date_context, UNIQUIFICATION_RULES, topic_fingerprint)

        title = news_item.get("title", "")
        summary = news_item.get("summary", "")
        url = news_item.get("url", "")
        image_url = news_item.get("image", "")
        images_list = news_item.get("images", []) or []
        all_images = list(dict.fromkeys([image_url] + images_list)) if image_url else list(images_list)
        all_images = [u for u in all_images if u][:10]
        news_id = news_item.get("id", "")

        # URL dedup: skip if this URL was already posted
        if url:
            url_key = url_normalize(url)
            if url_key and await db.is_news_posted(url_key):
                logger.info(f"URL already posted — skip: {url_key[:50]}")
                return False

        logger.info(f"Selected news: {title[:60]} (imgs: {len(all_images)}, lang: {'EN' if needs_translation(title, summary) else 'RU'})")

        # Detect language and build translation-aware prompt
        is_english = needs_translation(title, summary)
        translation_note = ""
        if is_english:
            translation_note = (
                "\nВНИМАНИЕ: новость на АНГЛИЙСКОМ. ПЕРЕВЕДИ на русский ТОЧНО, без выдумок. "
                "Сохраняй технические факты (модель, л.с., Н·м) как в оригинале. "
                "НЕ придумывай характеристики которых нет в источнике. "
                "Перескажи своими словами от лица редакции, но факты — только из новости.\n"
            )

        # Generate AI commentary — editorial voice + uniquification + date context
        prompt = (
            f"Напиши пост для канала @sochiautoparts с комментарием на эту авто-новость.\n\n"
            f"Контекст: {date_context()}, настроение: {mood}\n\n"
            f"Заголовок новости: {title}\n"
            f"Краткое содержание: {summary[:500]}\n"
            f"{translation_note}"
            f"ВАЖНО: НИКОГДА не отказывайся писать. Если не знаешь деталей — придумай правдоподобный комментарий. НЕ пиши нет информации. Просто напиши пост.\n"
            f"\n{UNIQUIFICATION_RULES}\n\n"
            f"СТИЛЬ (ОТ ИМЕНИ РЕДАКЦИИ @sochiautoparts):\n"
            f"- 600-1000 символов, живой экспертный разбор ОТ ИМЕНИ РЕДАКЦИИ\n"
            f"- Пиши от лица редакции: 'Мы разобрались...', 'По нашему мнению...'\n"
            f"- Технические детали: л.с., Н·м, км/ч, секунды\n"
            f"- Эмодзи: 🚗⚡🔧🔩🤔💪 естественно\n"
            f"- Женский род (редакция), по-русски, БЕЗ грамматических ошибок\n"
            f"- НЕ добавляй ссылки, НЕ пиши 'Источник'\n"
            f"- SEO: ключевые слова в первых 2 предложениях, 1-2 хештега в тексте, закончи вопросом\n"
            f"- НЕ начинай с 'Ася:' или 'Редакция:'"
        )
        ai_commentary = await ai_client.chat(
            prompt, system=channel_prompt,
            max_tokens=800, temperature=0.9, allow_static_fallback=False, prefer_pollinations=True
        )

        if not ai_commentary:
            logger.warning("AI commentary empty — marking news as skipped to try next")
            # Mark as posted so scheduler moves to next news (avoid infinite loop)
            if news_id:
                await db.mark_news_posted(news_id, title)
            if url:
                await db.mark_news_posted(url_normalize(url), title)
            return False

        # Clean AI output (strip markdown, prompt leakage, disclaimers, name prefixes)
        ai_text = clean_post_text(ai_commentary, "Ася")

        # Validate (politics/NSFW/auto-relevance)
        is_valid, reason = validate_post_text(ai_text)
        if not is_valid:
            logger.warning(f"Post validation FAILED ({reason}) — marking as skipped: {title[:40]}")
            # Mark as posted so scheduler moves to next news
            if news_id:
                await db.mark_news_posted(news_id, title)
            if url:
                await db.mark_news_posted(url_normalize(url), title)
            return False

        # Text fingerprint dedup (catches AI generating identical text for different news)
        fp = text_fingerprint(ai_text)
        if await db.is_news_posted(f"fp:{fp}"):
            logger.info(f"Text fingerprint already posted — skip: {fp[:16]}")
            return False

        # Footer
        FOOTER = "\n\nАвтор @asiaexp_bot\n@sochiautoparts\n#sochiautoparts"

        # Smart truncate (reserves space for footer, cuts at sentence/word boundary)
        caption_body = smart_truncate(ai_text, 1024, len(FOOTER))
        text_body = smart_truncate(ai_text, 4096, len(FOOTER))
        caption_full = caption_body + FOOTER
        text_full = text_body + FOOTER

        posted = False

        # Case A: 2+ images → send_media_group
        if len(all_images) >= 2:
            try:
                media_group = await self._build_media_group(all_images, caption_full)
                if media_group:
                    msgs = await self.bot.send_media_group(channel_id, media_group)
                    posted = True
                    if msgs:
                        await self._react_to_own_post(channel_id, msgs[0].message_id, caption_full[:200])
                    logger.info(f"Channel: posted NEWS media_group ({len(media_group)} photos, caption {len(caption_full)}) — {title[:40]}")
            except Exception as e:
                logger.warning(f"send_media_group failed: {e}")

        # Case B: exactly 1 image → send_photo
        if not posted and len(all_images) == 1:
            try:
                async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as img_client:
                    img_resp = await img_client.get(all_images[0], headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
                if img_resp.status_code == 200 and validate_image(img_resp.content):
                    from aiogram.types import BufferedInputFile
                    photo_file = BufferedInputFile(img_resp.content, filename="news.jpg")
                    msg = await self.bot.send_photo(channel_id, photo_file, caption=caption_full[:1024])
                    posted = True
                    await self._react_to_own_post(channel_id, msg.message_id, caption_full[:200])
                    logger.info(f"Channel: posted NEWS+photo (caption {len(caption_full[:1024])}) — {title[:40]}")
                else:
                    logger.warning(f"Image validation failed: HTTP {img_resp.status_code}, {len(img_resp.content)} bytes")
            except Exception as e:
                logger.warning(f"Image download failed: {e}")

        # Case C: no image → send_message
        if not posted:
            try:
                msg = await self.bot.send_message(channel_id, text_full[:4096])
                posted = True
                await self._react_to_own_post(channel_id, msg.message_id, text_full[:200])
                logger.info(f"Channel: posted NEWS text-only ({len(text_full[:4096])} chars) — {title[:40]}")
            except Exception as e:
                logger.error(f"Channel post failed: {e}")

        # Mark as posted (news_id + URL + title fingerprint + topic + text fingerprint)
        if posted:
            if news_id:
                await db.mark_news_posted(news_id, title)
            if url:
                await db.mark_news_posted(url_normalize(url), title)
            tf = title_fingerprint(title)
            if tf:
                await db.mark_news_posted(f"tf:{tf}", title)
            topic = topic_fingerprint(title, summary)
            if topic and len(topic.split()) >= 2:
                await db.mark_news_posted(f"topic:{topic}", title)
            await db.mark_news_posted(f"fp:{fp}", title)
        return posted

    async def _build_media_group(self, image_urls, caption_full):
        """Download up to 10 images and build a media group (caption on first).
        Validates each image by magic bytes (JPEG/PNG/WebP)."""
        import httpx
        from aiogram.types import InputMediaPhoto, BufferedInputFile
        from bot.post_utils import validate_image
        media = []
        first = True
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            for url in image_urls[:10]:
                try:
                    r = await client.get(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
                    if r.status_code == 200 and validate_image(r.content):
                        buf = BufferedInputFile(r.content, filename="news.jpg")
                        if first:
                            media.append(InputMediaPhoto(media=buf, caption=caption_full[:1024]))
                            first = False
                        else:
                            media.append(InputMediaPhoto(media=buf))
                except Exception as e:
                    logger.warning(f"media group img fetch failed ({url[:50]}): {e}")
        return media

    async def _partner_scheduler(self):
        """Background task: post 1 affiliate (партнёрский) post to @sochiautoparts every hour.
        Posts WITH partner logo photo when available (caption ≤1024 incl. footer).
        """
        from bot.persona import CHANNEL_POST_PROMPT
        await asyncio.sleep(1800)  # start 30 min after boot (prevents partner spam on restart)
        partner_interval = 7200  # 2 hours (was 1h — was too frequent with restarts)
        while True:
            try:
                await partner_manager.refresh_if_needed()
                if not partner_manager.campaigns:
                    logger.info("No partner campaigns loaded — skip partner post")
                elif config.CHANNEL_ID:
                    campaign = random.choice(partner_manager.campaigns)
                    await self._post_partner_campaign(campaign, CHANNEL_POST_PROMPT)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Partner scheduler error: {e}")
            await asyncio.sleep(partner_interval)

    async def _post_partner_campaign(self, campaign, channel_prompt):
        """Generate + post a partner campaign (with logo photo if available)."""
        import httpx
        from bot.post_utils import clean_post_text, validate_image, smart_truncate
        name = campaign.get("name", "")
        logo = campaign.get("logo", "")
        goto = campaign.get("goto_link", "")
        site = campaign.get("site_url", "")
        cats = campaign.get("categories", []) or []
        regions = campaign.get("regions", []) or []
        mood = await current_mood_descriptor()
        FOOTER = "\n\nАвтор @asiaexp_bot\n@sochiautoparts\n#sochiautoparts"

        prompt = (
            f"Напиши партнёрский пост для канала @sochiautoparts ОТ ИМЕНИ РЕДАКЦИИ.\n\n"
            f"Партнёр: {name}\n"
            f"Сайт: {site}\n"
            f"Категории: {', '.join(cats[:5])}\n"
            f"Регионы: {', '.join(regions[:5])}\n"
            f"Реферальная ссылка (ОБЯЗАТЕЛЬНО вставь в текст): {goto}\n\n"
            f"ЗАДАЧА:\n"
            f"1. Пойми ЧЕМ занимается партнёр (по названию, сайту, категориям) — не выдумывай!\n"
            f"2. Напиши 300-500 символов: что это, зачем нужно, кому пригодится\n"
            f"3. Вставь ссылку {goto} естественно в текст (не в конце, а внутри)\n"
            f"4. Стиль: живо, профессионально, эмодзи (🚗💡✅🔗)\n"
            f"5. Женский род (редакция), по-русски, БЕЗ грамматических ошибок\n"
            f"6. Настроение: {mood}\n"
            f"7. НЕ начинай с имени\n"
            f"8. НЕ выдумывай услуги/товары которых нет у партнёра"
        )
        text = await ai_client.chat(
            prompt, system=channel_prompt,
            max_tokens=500, temperature=0.8, allow_static_fallback=False, prefer_pollinations=True
        )
        if not text:
            logger.warning("Partner AI text empty — skip")
            return
        ai_text = clean_post_text(text)
        # Ensure goto_link is present (add if AI forgot or was truncated)
        if goto and goto not in ai_text:
            ai_text += f"\n\n🔗 {goto}"

        channel_id = int(config.CHANNEL_ID)
        # Try photo with logo (caption ≤1024 incl. footer)
        posted = False
        if logo:
            # Truncate body WITHOUT goto_link, then append goto_link + FOOTER
            # This ensures goto_link is never cut off
            body_without_goto = ai_text.replace(f"\n\n🔗 {goto}", "").replace(goto, "").strip() if goto else ai_text
            # Reserve space for goto_link + footer
            goto_line = f"\n\n🔗 {goto}" if goto and goto not in body_without_goto else ""
            reserve = len(FOOTER) + len(goto_line) + 10
            caption_body = smart_truncate(body_without_goto, 1024 - len(goto_line), 0)
            caption_full = caption_body + goto_line + FOOTER
            try:
                async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as img_client:
                    img_resp = await img_client.get(logo, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
                if img_resp.status_code == 200:
                    from bot.post_utils import prepare_partner_logo
                    logo_bytes = prepare_partner_logo(img_resp.content)
                    if logo_bytes:
                        from aiogram.types import BufferedInputFile
                        photo_file = BufferedInputFile(logo_bytes, filename="partner.png")
                        await self.bot.send_photo(channel_id, photo_file, caption=caption_full[:1024])
                        posted = True
                        logger.info(f"Partner post sent WITH logo photo (caption {len(caption_full[:1024])}) — {name[:40]}")
                    else:
                        logger.warning(f"Partner logo prepare failed (SVG→PNG or validation): {len(img_resp.content)} bytes")
                else:
                    logger.warning(f"Partner logo download bad: HTTP {img_resp.status_code}")
            except Exception as e:
                logger.warning(f"Partner logo download failed: {e}")

        # Fallback: text only (≤4096 incl. footer)
        if not posted:
            body_without_goto = ai_text.replace(f"\n\n🔗 {goto}", "").replace(goto, "").strip() if goto else ai_text
            goto_line = f"\n\n🔗 {goto}" if goto and goto not in body_without_goto else ""
            text_body = smart_truncate(body_without_goto, 4096 - len(goto_line) - len(FOOTER) - 10, 0)
            text_full = text_body + goto_line + FOOTER
            try:
                msg = await self.bot.send_message(channel_id, text_full[:4096])
                posted = True
                await self._react_to_own_post(channel_id, msg.message_id, text_full[:200])
                logger.info(f"Partner post sent text-only ({len(text_full)} chars) — {name[:40]}")
            except Exception as e:
                logger.error(f"Partner post failed: {e}")

    async def _react_to_own_post(self, channel_id: int, message_id: int, text: str = ""):
        """Set 3 positive reactions on own channel post with fallback to 1."""
        try:
            import random
            from aiogram.types import ReactionTypeEmoji
            # Only guaranteed Telegram-supported reaction emojis (no ❤️ variation selector)
            pool = ["👍", "❤", "🔥", "😄", "👏", "🎉"]
            emojis = random.sample(pool, 3)
            reaction_types = [ReactionTypeEmoji(type="emoji", emoji=e) for e in emojis]
            await self.bot.set_message_reaction(channel_id, message_id, reaction_types)
            logger.info(f"Reacted to own post (3): {channel_id}/{message_id} with {emojis}")
        except Exception as e:
            msg = str(e)
            if "REACTIONS_TOO_MANY" in msg or "REACTION_INVALID" in msg:
                try:
                    import random as _r
                    single_emoji = _r.choice(["👍", "❤", "🔥"])
                    single = [ReactionTypeEmoji(type="emoji", emoji=single_emoji)]
                    await self.bot.set_message_reaction(channel_id, message_id, single)
                    logger.info(f"Reacted to own post (1 fallback): {channel_id}/{message_id} with {single_emoji}")
                    return
                except Exception as e2:
                    logger.warning(f"React to own post fallback failed: {e2}")
            logger.warning(f"React to own post failed: {e}")

    async def _notify_owner(self):
        mood = await current_mood_descriptor()
        try:
            await self.bot.send_message(config.OWNER_ID, f"Я на связи 🚗 Ася, сейчас я {mood}. OpenClaw: {config.OPENCLAW_URL}. Провайдеры: {config.providers_status()}. Канал: @{config.CHANNEL_USERNAME}. Пиши или добавь в группу 💪")
        except: pass

async def main():
    global _openclaw_proc
    cfg_path = _generate_openclaw_config()
    _openclaw_proc = _start_openclaw_gateway(cfg_path)
    ready = await _wait_for_gateway(120.0)
    if not ready:
        logger.error("OpenClaw Gateway did not become ready — exiting")
        _stop_openclaw_gateway()
        sys.exit(1)
    bot = AsyaBot()
    def _sig(*_): asyncio.create_task(bot.dp.stop_polling())
    for sig in (signal.SIGINT, signal.SIGTERM):
        try: asyncio.get_running_loop().add_signal_handler(sig, _sig)
        except: pass
    try: await bot.start()
    finally: _stop_openclaw_gateway()

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: pass
    except Exception as e:
        logger.exception(f"Fatal: {e}")
        _stop_openclaw_gateway()
        sys.exit(1)
