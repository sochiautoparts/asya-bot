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
        """Background task: periodically post auto content to @sochiautoparts.
        Alternates between RSS auto news (with AI commentary) and AI-generated posts.
        """
        from bot.persona import CHANNEL_POST_PROMPT
        await asyncio.sleep(120)
        post_interval = 1200  # 20 min
        cycle = 0
        while True:
            try:
                channel_id = int(config.CHANNEL_ID)
                mood = await current_mood_descriptor()
                cycle += 1

                # Every 3rd cycle: try to fetch and post real RSS auto news
                if cycle % 3 == 0:
                    news_posted = await self._fetch_and_post_news(channel_id, mood)
                    if news_posted:
                        await asyncio.sleep(post_interval)
                        continue

                # Otherwise: AI-generated post on auto topic
                topics = [
                    "новые авто-новости недели — главные события",
                    "обзор популярной модели — плюсы и минусы",
                    "как выбрать подержанный авто — советы эксперта",
                    "топ-5 проблем популярных двигателей",
                    "регламент ТО — что и когда менять",
                    "китайские авто — стоит ли покупать",
                    "немецкое качество vs японская надёжность",
                    "электромобили — тренд или будущее",
                    "зимняя резина — когда менять и какую выбирать",
                    "как проверить авто перед покупкой — чек-лист",
                    "расход топлива — как снизить",
                    "выбор между новой Lada и б/у иномаркой",
                    "кроссоверы 2025 — топ-5 моделей",
                    "автокредит или рассрочка — что выгоднее",
                ]
                topic = random.choice(topics)
                prompt = f"Напиши пост для канала @sochiautoparts на тему: {topic}. Настроение: {mood}. 3-5 предложений, живо, с эмодзи, авто-экспертиза."
                post = await ai_client.chat(prompt, system=CHANNEL_POST_PROMPT, fast=True, max_tokens=400, allow_static_fallback=False)
                if post:
                    post = post.strip()
                    if not post.endswith("@sochiautoparts"):
                        post += "\n\n🚗 @sochiautoparts"
                    await self.bot.send_message(channel_id, post[:4000])
                    logger.info(f"Channel: posted auto content ({len(post)} chars)")
            except asyncio.CancelledError: break
            except Exception as e:
                logger.error(f"Channel scheduler error: {e}")
            await asyncio.sleep(post_interval)

    async def _fetch_and_post_news(self, channel_id: int, mood: str) -> bool:
        """Fetch auto news from RSS feeds, write AI commentary, post to channel.
        Returns True if a news post was made."""
        import feedparser
        import httpx
        from bot.persona import CHANNEL_POST_PROMPT

        # Auto news RSS feeds (tested working)
        RSS_FEEDS = [
            "https://news.mail.ru/rss/auto/",
        ]

        try:
            # Fetch RSS feeds concurrently
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                feed_texts = []
                for url in RSS_FEEDS:
                    try:
                        resp = await client.get(url, headers={"User-Agent": "AsyaBot/1.0"})
                        if resp.status_code == 200:
                            feed_texts.append(resp.text)
                    except: pass

            if not feed_texts:
                logger.debug("No RSS feeds fetched")
                return False

            # Parse feeds and collect recent items
            news_items = []
            for feed_text in feed_texts:
                feed = feedparser.parse(feed_text)
                for entry in feed.entries[:5]:
                    title = entry.get("title", "").strip()
                    link = entry.get("link", "").strip()
                    summary = entry.get("summary", entry.get("description", "")).strip()
                    if title and len(title) > 10:
                        # Clean HTML from summary
                        import re
                        summary = re.sub(r"<[^>]+>", "", summary)[:200]
                        news_items.append({"title": title, "link": link, "summary": summary})

            if not news_items:
                logger.debug("No news items found in feeds")
                return False

            # Pick a random news item
            import random as _r
            news = _r.choice(news_items)
            logger.info(f"News fetched: {news['title'][:60]}")

            # Generate AI commentary on the news
            prompt = (
                f"Напиши пост для канала @sochiautoparts с комментарием на эту авто-новость:\n\n"
                f"Новость: {news['title']}\n"
                f"Кратко: {news['summary'][:200]}\n\n"
                f"Дай свой комментарий как автоэксперт, поделись мнением. "
                f"3-5 предложений, живо, с эмодзи. Настроение: {mood}. "
                f"Если уместно — добавь ссылку на источник."
            )
            post = await ai_client.chat(prompt, system=CHANNEL_POST_PROMPT, fast=True, max_tokens=400, allow_static_fallback=False)
            if post:
                post = post.strip()
                # Add source link if available
                if news["link"] and news["link"] not in post:
                    post += f"\n\n📖 Источник: {news['link']}"
                if not post.endswith("@sochiautoparts"):
                    post += "\n\n🚗 @sochiautoparts"
                await self.bot.send_message(channel_id, post[:4000])
                logger.info(f"Channel: posted NEWS content ({len(post)} chars) — {news['title'][:40]}")
                return True
        except Exception as e:
            logger.debug(f"news fetch error: {e}")
        return False

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
