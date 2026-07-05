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
        try: await self.bot.delete_webhook(drop_pending_updates=False)
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

                # 2. Find up to 2 unposted news items
                unposted = []
                for item in all_items:
                    news_id = item.get("id", "")
                    if news_id and not await db.is_news_posted(news_id):
                        unposted.append(item)
                        if len(unposted) >= 2:
                            break

                if not unposted:
                    # All items posted — reset (clear posted_news table)
                    logger.info("All news items already posted — resetting posted_news")
                    try:
                        conn = db._conn()
                        await conn.execute("DELETE FROM posted_news")
                        await conn.commit()
                    except: pass
                    unposted = all_items[:2]

                # 3. Post each item (up to 2 per cycle), with a short gap between
                for idx, news_item in enumerate(unposted):
                    try:
                        posted = await self._post_news_item(news_item, mood, channel_id, CHANNEL_POST_PROMPT)
                        if posted:
                            logger.info(f"Cycle: posted news {idx+1}/{len(unposted)}")
                    except Exception as e:
                        logger.error(f"Post news item error: {e}")
                    if idx < len(unposted) - 1:
                        await asyncio.sleep(60)  # small gap between the 2 posts

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Channel scheduler error: {e}")

            await asyncio.sleep(post_interval)

    async def _post_news_item(self, news_item, mood, channel_id, channel_prompt):
        """Post a single news item to channel with photo (editorial voice). Returns True if posted.

        Handles: 1 photo → send_photo; 2+ photos → send_media_group (caption on first);
        0 photos → send_message. Reserves space for footer to avoid truncation.
        """
        import httpx
        title = news_item.get("title", "")
        summary = news_item.get("summary", "")
        image_url = news_item.get("image", "")
        images_list = news_item.get("images", []) or []
        # Dedup image list, cap at 10 (Telegram media group limit)
        all_images = list(dict.fromkeys([image_url] + images_list)) if image_url else list(images_list)
        all_images = [u for u in all_images if u][:10]
        news_id = news_item.get("id", "")

        logger.info(f"Selected news: {title[:60]} (imgs: {len(all_images)})")

        # Generate AI commentary — editorial voice (от имени редакции)
        prompt = (
            f"Напиши пост для канала @sochiautoparts с комментарием на эту авто-новость.\n\n"
            f"Заголовок новости: {title}\n"
            f"Краткое содержание: {summary[:400]}\n\n"
            f"СТИЛЬ (ОТ ИМЕНИ РЕДАКЦИИ @sochiautoparts):\n"
            f"- 600-1200 символов, живой экспертный разбор ОТ ИМЕНИ РЕДАКЦИИ канала\n"
            f"- Пиши от лица редакции (коллектив автоэкспертов, бывший юрист, технари)\n"
            f"- Не от первого лица лично — а как позиция редакции: 'Редакция считает...', 'Мы разобрались...'\n"
            f"- Технические детали: л.с., Н·м, км/ч, секунды, размеры\n"
            f"- Эмодзи: 🚗⚡🔧🔩🤔💪 естественно\n"
            f"- Женский род (редакция), по-русски\n"
            f"- Настроение: {mood}\n"
            f"- НЕ копируй новость — пиши СВОЙ комментарий\n"
            f"- НЕ добавляй ссылки, НЕ пиши 'Источник'\n"
            f"- НЕ начинай с 'Ася:' или 'Редакция:'"
        )
        ai_commentary = await ai_client.chat(
            prompt, system=channel_prompt,
            max_tokens=800, temperature=0.9, allow_static_fallback=False
        )

        if not ai_commentary:
            logger.warning("AI commentary empty — skipping post")
            return False

        # Footer (constant)
        FOOTER = "\n\nАвтор @asiaexp_bot\n@sochiautoparts\n#sochiautoparts"
        ai_text = ai_commentary.strip()

        # Reserve space for footer: caption ≤ 1024, text ≤ 4096
        caption_limit = 1024 - len(FOOTER) - 5
        text_limit = 4096 - len(FOOTER) - 5
        caption_body = ai_text[:caption_limit]
        text_body = ai_text[:text_limit]
        caption_full = caption_body + FOOTER
        text_full = text_body + FOOTER

        posted = False

        # Case A: 2+ images → send_media_group (caption on first photo only, ≤1024)
        if len(all_images) >= 2:
            try:
                media_group = await self._build_media_group(all_images, caption_full)
                if media_group:
                    await self.bot.send_media_group(channel_id, media_group)
                    posted = True
                    logger.info(f"Channel: posted NEWS media_group ({len(media_group)} photos, caption {len(caption_full)}) — {title[:40]}")
            except Exception as e:
                logger.warning(f"send_media_group failed: {e}")

        # Case B: exactly 1 image → send_photo (caption ≤1024)
        if not posted and len(all_images) == 1:
            try:
                async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as img_client:
                    img_resp = await img_client.get(all_images[0], headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
                if img_resp.status_code == 200 and len(img_resp.content) > 2000:
                    from aiogram.types import BufferedInputFile
                    photo_file = BufferedInputFile(img_resp.content, filename="news.jpg")
                    await self.bot.send_photo(channel_id, photo_file, caption=caption_full)
                    posted = True
                    logger.info(f"Channel: posted NEWS+photo (caption {len(caption_full)}) — {title[:40]}")
                else:
                    logger.warning(f"Image download bad: HTTP {img_resp.status_code}, {len(img_resp.content)} bytes")
            except Exception as e:
                logger.warning(f"Image download failed: {e}")

        # Case C: no image (or all image downloads failed) → send_message (≤4096)
        if not posted:
            try:
                await self.bot.send_message(channel_id, text_full[:4096])
                posted = True
                logger.info(f"Channel: posted NEWS text-only ({len(text_full)} chars) — {title[:40]}")
            except Exception as e:
                logger.error(f"Channel post failed: {e}")

        if posted and news_id:
            await db.mark_news_posted(news_id, title)
        return posted

    async def _build_media_group(self, image_urls, caption_full):
        """Download up to 10 images and build a media group (caption on first)."""
        import httpx
        from aiogram.types import InputMediaPhoto, BufferedInputFile
        media = []
        first = True
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            for url in image_urls[:10]:
                try:
                    r = await client.get(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
                    if r.status_code == 200 and len(r.content) > 2000:
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
        await asyncio.sleep(300)  # start 5 min after boot
        partner_interval = 3600  # 1 hour
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
        name = campaign.get("name", "")
        logo = campaign.get("logo", "")
        goto = campaign.get("goto_link", "")
        cats = campaign.get("categories", []) or []
        regions = campaign.get("regions", []) or []
        mood = await current_mood_descriptor()
        FOOTER = "\n\nАвтор @asiaexp_bot\n@sochiautoparts\n#sochiautoparts"

        prompt = (
            f"Напиши партнёрский пост для канала @sochiautoparts ОТ ИМЕНИ РЕДАКЦИИ.\n\n"
            f"Партнёр: {name}\n"
            f"Категории: {', '.join(cats[:5])}\n"
            f"Регионы: {', '.join(regions[:5])}\n"
            f"Ссылка: {goto}\n\n"
            f"СТИЛЬ (от имени редакции @sochiautoparts):\n"
            f"- 400-700 символов, рекомендация партнёра от лица редакции\n"
            f"- Что это, зачем нужно, кому пригодится, почему редакция советует\n"
            f"- Живо, профессионально, с эмодзи (🚗💡✅🔗)\n"
            f"- Обязательно вставь ссылку {goto} прямо в текст\n"
            f"- Женский род (редакция), по-русски\n"
            f"- Настроение: {mood}\n"
            f"- НЕ начинай с 'Ася:' или 'Редакция:'"
        )
        text = await ai_client.chat(
            prompt, system=channel_prompt,
            max_tokens=500, temperature=0.8, allow_static_fallback=False
        )
        if not text:
            logger.warning("Partner AI text empty — skip")
            return
        ai_text = text.strip()
        if goto and goto not in ai_text:
            ai_text += f"\n\n🔗 {goto}"

        channel_id = int(config.CHANNEL_ID)
        # Try photo with logo (caption ≤1024 incl. footer)
        posted = False
        if logo:
            caption_limit = 1024 - len(FOOTER) - 5
            caption_full = ai_text[:caption_limit] + FOOTER
            try:
                async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as img_client:
                    img_resp = await img_client.get(logo, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
                if img_resp.status_code == 200 and len(img_resp.content) > 2000:
                    from aiogram.types import BufferedInputFile
                    photo_file = BufferedInputFile(img_resp.content, filename="partner.jpg")
                    await self.bot.send_photo(channel_id, photo_file, caption=caption_full[:1024])
                    posted = True
                    logger.info(f"Partner post sent WITH logo photo (caption {len(caption_full[:1024])}) — {name[:40]}")
                else:
                    logger.warning(f"Partner logo download bad: HTTP {img_resp.status_code}, {len(img_resp.content)} bytes")
            except Exception as e:
                logger.warning(f"Partner logo download failed: {e}")

        # Fallback: text only (≤4096 incl. footer)
        if not posted:
            text_limit = 4096 - len(FOOTER) - 5
            text_full = ai_text[:text_limit] + FOOTER
            try:
                await self.bot.send_message(channel_id, text_full[:4096])
                posted = True
                logger.info(f"Partner post sent text-only ({len(text_full)} chars) — {name[:40]}")
            except Exception as e:
                logger.error(f"Partner post failed: {e}")

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
