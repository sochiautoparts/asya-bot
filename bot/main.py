"""
Asya Bot Main Entry Point — @asiaexp_bot
Ася — Автоэксперт, ведёт канал @sochiautoparts

Features:
- aiogram 3.x Telegram Bot framework
- Pollinations AI as primary provider
- SQLite with aiosqlite for persistence
- Background tasks: news fetching, channel posting
- Singleton lock to prevent duplicate instances
- Conflict resolution for webhook/polling
"""

import asyncio
import logging
import os
import signal
import sys
import time
import fcntl
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from bot.config import config
from bot.database import init_db
from bot.handlers import get_all_routers
from bot.partners import partner_manager
from ai.router import ai_router
from news import run_news_cycle
from channel import channel_manager
from miniapp.server import start_miniapp_server

# ── Logging setup ──────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("asya.main")

# Reduce noisy loggers
for noisy in ["aiogram.event", "httpx", "httpcore", "aiosqlite"]:
    logging.getLogger(noisy).setLevel(logging.WARNING)


# ── Singleton Lock ─────────────────────────────────────────────────────────────

class SingletonLock:
    """File-based lock to prevent multiple bot instances."""

    def __init__(self, lock_file: str):
        self.lock_file = lock_file
        self._lock_fd = None

    def acquire(self) -> bool:
        """Try to acquire the lock. Returns True if successful."""
        try:
            os.makedirs(os.path.dirname(self.lock_file) or ".", exist_ok=True)
            self._lock_fd = open(self.lock_file, "w")
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._lock_fd.write(str(os.getpid()))
            self._lock_fd.flush()
            return True
        except (IOError, OSError):
            if self._lock_fd:
                self._lock_fd.close()
                self._lock_fd = None
            return False

    def release(self) -> None:
        """Release the lock."""
        if self._lock_fd:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                self._lock_fd.close()
                os.unlink(self.lock_file)
            except (IOError, OSError):
                pass
            self._lock_fd = None


# ── Background Tasks ───────────────────────────────────────────────────────────

class BackgroundTasks:
    """Manages background tasks for news and channel posting."""

    def __init__(self, bot: Bot):
        self.bot = bot
        self._running = False
        self._tasks: list = []
        self._greeting_sent = False

    async def start(self) -> None:
        """Start all background tasks."""
        self._running = True
        self._tasks = [
            asyncio.create_task(self._morning_greeting(), name="morning_greeting"),
            asyncio.create_task(self._news_fetcher(), name="news_fetcher"),
            asyncio.create_task(self._channel_poster(), name="channel_poster"),
        ]
        logger.info("Background tasks started")

    async def stop(self) -> None:
        """Stop all background tasks."""
        self._running = False
        for task in self._tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()
        logger.info("Background tasks stopped")

    async def _morning_greeting(self) -> None:
        """Send a natural 'just woke up' greeting to the owner — like a living person, not a bot."""
        if self._greeting_sent:
            return

        await asyncio.sleep(15)  # Wait a bit after startup
        self._greeting_sent = True

        try:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            import random
            hour = datetime.now(ZoneInfo("Europe/Moscow")).hour

            if 5 <= hour < 12:
                greetings = [
                    "Ммм... проснулась ☀️ Кофе, душ, и я готова к новому дню!",
                    "Доброе утро! ☀️ Еле глаза открыла... Зато уже с вами!",
                    "Утро! ☕ Как спалось? Я вот только-только встала",
                    "Привет! ☀️ Доброе утро! Кофе уже заварила, можно общаться",
                ]
            elif 12 <= hour < 18:
                greetings = [
                    "Привет! 😊 День в самом разгаре — я тут, на связи",
                    "Добрый день! ☕ Перекусила и готова болтать",
                    "Хей! 😊 Как день проходит? Я вот решила заглянуть поболтать",
                    "Привет! Дневная Ася на связи 😊 Чем займёмся?",
                ]
            elif 18 <= hour < 23:
                greetings = [
                    "Добрый вечер! 🌆 Устала за день, но для вас всегда время найду",
                    "Вечер! 🌆 Сидим, общаемся — мне нравится такой формат",
                    "Привет! 🌆 Вечер — лучшее время для спокойных разговоров",
                    "Хей! 🌆 Заканчиваю свои дела, можно и поболтать",
                ]
            else:
                greetings = [
                    "О, ты ещё не спишь? 🌙 Я вот тоже сова оказалась",
                    "Ночной режим активирован 🌙 Тихо, спокойно — самое то для разговоров",
                    "Доброй ночи! 🌙 Хотя... кого я обманываю, спать пока не хочется",
                    "Привет полуночный! 🌙 Не спится? Понимаю, мне тоже",
                ]

            greeting = random.choice(greetings)
            if config.OWNER_ID:
                await self.bot.send_message(config.OWNER_ID, greeting)
        except Exception as e:
            logger.debug(f"Morning greeting error: {e}")

    async def _news_fetcher(self) -> None:
        """Periodically fetch news from RSS sources."""
        # Initial fetch shortly after startup
        await asyncio.sleep(30)

        while self._running:
            try:
                count = await run_news_cycle()
                if count > 0:
                    logger.info(f"News fetcher: {count} new items")
            except Exception as e:
                logger.error(f"News fetcher error: {e}")

            # Wait for next cycle
            interval = config.NEWS_INTERVAL_MINUTES * 60
            for _ in range(interval):
                if not self._running:
                    break
                await asyncio.sleep(1)

    async def _channel_poster(self) -> None:
        """Periodically post to channel.
        
        Uses config.CHANNEL_POST_INTERVAL_MINUTES for the interval.
        """
        # Wait a bit after startup
        await asyncio.sleep(30)

        while self._running:
            try:
                posted = await channel_manager.run_scheduled_post()
                if posted:
                    logger.info("Channel poster: posted successfully")
            except Exception as e:
                logger.error(f"Channel poster error: {e}")

            # Wait for next cycle — check every configured interval
            interval = config.CHANNEL_POST_INTERVAL_MINUTES * 60
            for _ in range(interval):
                if not self._running:
                    break
                await asyncio.sleep(1)


# ── Conflict Resolution ────────────────────────────────────────────────────────

async def resolve_conflicts(bot: Bot) -> None:
    """Resolve any conflicts (webhooks, other instances)."""
    try:
        # Delete any existing webhook
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Webhook deleted, switching to polling")
    except Exception as e:
        logger.warning(f"Error resolving conflicts: {e}")


# ── Startup / Shutdown ─────────────────────────────────────────────────────────

async def on_startup(bot: Bot) -> None:
    """Actions to perform on bot startup."""
    logger.info("=" * 50)
    logger.info("Asya Bot (@asiaexp_bot) starting up...")
    logger.info(f"Channel: {config.CHANNEL_ID}")
    logger.info("=" * 50)

    # Initialize database
    await init_db()
    logger.info("Database initialized")

    # Initialize AI router
    await ai_router.initialize()
    logger.info("AI router initialized")

    # Load partner programs
    partner_count = partner_manager.load()
    logger.info(f"Partner programs loaded: {partner_count}")

    # Set bot in channel manager
    channel_manager.set_bot(bot)
    logger.info("Channel manager initialized")

    # Resolve conflicts
    await resolve_conflicts(bot)

    # Start Mini App server (web app for Telegram)
    try:
        await start_miniapp_server(port=int(os.getenv("MINIAPP_PORT", "8080")))
        logger.info("Mini App server started")
    except Exception as e:
        logger.warning(f"Mini App server failed to start: {e}")

    # Set bot commands
    from aiogram.types import BotCommand
    commands = [
        BotCommand(command="start", description="Приветствие"),
        BotCommand(command="help", description="Справка"),
        BotCommand(command="app", description="Открыть мини-приложение"),
        BotCommand(command="clear", description="Очистить историю"),
        BotCommand(command="diagnostic", description="Режим диагностики"),
        BotCommand(command="parts", description="Поиск запчастей"),
        BotCommand(command="normal", description="Обычный режим"),
        BotCommand(command="mycar", description="Мои машины"),
        BotCommand(command="delcar", description="Удалить машину"),
        BotCommand(command="mileage", description="Обновить пробег"),
        BotCommand(command="admin", description="Панель админа"),
        BotCommand(command="switch", description="Сменить AI модель"),
    ]
    await bot.set_my_commands(commands)
    logger.info("Bot commands set")

    # NO startup notification to owner — technical info stays in logs only
    # Asya "wakes up" naturally via the morning greeting background task


async def on_shutdown(bot: Bot) -> None:
    """Actions to perform on bot shutdown."""
    logger.info("Asya Bot shutting down...")

    # Quiet shutdown — no technical messages to chat
    # Close bot session
    await bot.session.close()


# ── Main ───────────────────────────────────────────────────────────────────────

async def main() -> None:
    """Main entry point."""
    # Singleton lock
    lock = SingletonLock(config.LOCK_FILE)
    if not lock.acquire():
        logger.error("Another instance is already running. Exiting.")
        sys.exit(1)

    try:
        # Create bot
        bot = Bot(
            token=config.BOT_TOKEN,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )

        # Create dispatcher
        dp = Dispatcher(storage=MemoryStorage())

        # Register routers
        dp.include_router(get_all_routers())

        # Startup / shutdown hooks
        dp.startup.register(on_startup)
        dp.shutdown.register(on_shutdown)

        # Background tasks
        bg_tasks = BackgroundTasks(bot)

        # Override signal handlers for graceful shutdown
        loop = asyncio.get_event_loop()

        def signal_handler(sig, frame):
            logger.info(f"Received signal {sig}, shutting down...")
            asyncio.ensure_future(bg_tasks.stop())

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, lambda s=sig: signal_handler(s, None))
            except NotImplementedError:
                pass  # Windows doesn't support add_signal_handler

        # Start background tasks after startup
        @dp.startup()
        async def start_background(bot: Bot):
            await bg_tasks.start()

        # Start polling
        logger.info("Starting polling...")
        try:
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
        finally:
            await bg_tasks.stop()
            await on_shutdown(bot)

    finally:
        lock.release()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by keyboard interrupt")
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
