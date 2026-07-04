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
        # Auto Channel scheduler — Ася posts to @sochiautoparts
        if config.CHANNEL_ID:
            asyncio.create_task(self._channel_scheduler(), name="channel_scheduler")
            logger.info(f"Channel scheduler enabled (@{config.CHANNEL_USERNAME})")
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
        """Background task: post auto news from auto-news.json to @sochiautoparts.
        Fetches from sochiautoparts/nws repo (self-updating source), extracts photos,
        generates AI commentary, posts with photo to channel.
        """
        from bot.persona import CHANNEL_POST_PROMPT
        await asyncio.sleep(120)
        post_interval = 1200  # 20 min
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

                # 2. Find unposted news item
                news_item = None
                for item in all_items:
                    news_id = item.get("id", "")
                    if news_id and not await db.is_news_posted(news_id):
                        news_item = item
                        break

                if not news_item:
                    # All items posted — reset (clear posted_news table)
                    logger.info("All news items already posted — resetting posted_news")
                    try:
                        conn = db._conn()
                        await conn.execute("DELETE FROM posted_news")
                        await conn.commit()
                    except: pass
                    news_item = all_items[0]

                title = news_item.get("title", "")
                summary = news_item.get("summary", "")
                url = news_item.get("url", "")
                image_url = news_item.get("image", "")
                images_list = news_item.get("images", []) or []
                # Use up to 10 images (Telegram media group limit)
                all_images = list(dict.fromkeys([image_url] + images_list))[:10] if image_url else images_list[:10]
                all_images = [u for u in all_images if u][:10]
                source = news_item.get("source", "")
                news_id = news_item.get("id", "")

                logger.info(f"Selected news: {title[:60]} (img: {'yes' if image_url else 'no'})")

                # 3. Generate AI commentary — like the old bot did
                # Old format: 500-1000 chars, expert opinion, personal touch, emoji
                prompt = (
                    f"Напиши пост для канала @sochiautoparts с комментарием на эту авто-новость.\n\n"
                    f"Заголовок новости: {title}\n"
                    f"Краткое содержание: {summary[:400]}\n\n"
                    f"Требования:\n"
                    f"- Напиши СВОЙ комментарий как автоэксперт (НЕ копируй новость!)\n"
                    f"- 500-900 символов, живой, экспертный, с мнением\n"
                    f"- Эмодзи уместно (🚗⚡🔥✨🤔💪)\n"
                    f"- Упомяни технические детали если есть\n"
                    f"- Поделись личным мнением как Ася (бывший юрист, автоэксперт)\n"
                    f"- Женский род, по-русски\n"
                    f"- Настроение: {mood}\n"
                    f"- НЕ добавляй ссылки и НЕ пиши «Источник»\n"
                    f"- НЕ начинай с «Ася:»"
                )
                ai_commentary = await ai_client.chat(
                    prompt, system=CHANNEL_POST_PROMPT,
                    max_tokens=800, temperature=0.9, allow_static_fallback=False
                )

                if not ai_commentary:
                    logger.warning("AI commentary empty — skipping post")
                    await asyncio.sleep(post_interval)
                    continue

                # 4. Build post text — match old format: just AI text + footer
                post_text = ai_commentary.strip()[:3000]
                # Footer: 🚗 @sochiautoparts (no source link — matches old format)
                if not post_text.endswith("@sochiautoparts"):
                    post_text += "\n\nАвтор @asiaexp_bot\n@sochiautoparts\n#sochiautoparts"

                # 5. Download image and post with photo
                posted = False
                if image_url:
                    try:
                        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as img_client:
                            img_resp = await img_client.get(image_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
                        if img_resp.status_code == 200 and len(img_resp.content) > 2000:
                            from aiogram.types import BufferedInputFile
                            photo_file = BufferedInputFile(img_resp.content, filename="news.jpg")
                            # Caption limit is 1024 for photos
                            caption = post_text[:1024]
                            await self.bot.send_photo(channel_id, photo_file, caption=caption)
                            posted = True
                            logger.info(f"Channel: posted NEWS+photo ({len(post_text)} chars, caption {len(caption)}) — {title[:40]}")
                        else:
                            logger.warning(f"Image download bad status/size: HTTP {img_resp.status_code}, {len(img_resp.content)} bytes")
                    except Exception as e:
                        logger.warning(f"Image download failed: {e}")

                # 6. Fallback: post text only (no image) — full text up to 4096
                if not posted:
                    try:
                        await self.bot.send_message(channel_id, post_text[:4096])
                        posted = True
                        logger.info(f"Channel: posted NEWS text-only ({len(post_text)} chars) — {title[:40]}")
                    except Exception as e:
                        logger.error(f"Channel post failed: {e}")

                # 7. Mark news as posted
                if posted and news_id:
                    await db.mark_news_posted(news_id, title)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Channel scheduler error: {e}")

            await asyncio.sleep(post_interval)

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
