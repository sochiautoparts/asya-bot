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
import random
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
from bot.database import init_db, cleanup_old_fingerprints
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
        """Send a natural greeting to the owner — like a living person, not a bot.
        Only sends ONCE per startup. Short and varied. Has a 4-hour cooldown
        to prevent spam on frequent restarts."""
        if self._greeting_sent:
            return

        await asyncio.sleep(15)  # Wait a bit after startup
        self._greeting_sent = True

        # Cooldown: don't send if one was sent recently (within 4 hours)
        try:
            cooldown_file = "/tmp/asya_last_greeting"
            import os
            if os.path.exists(cooldown_file):
                with open(cooldown_file, "r") as f:
                    last_greeting_time = float(f.read().strip())
                if time.time() - last_greeting_time < 14400:  # 4 hours
                    logger.info("Greeting cooldown active — skipping")
                    return
        except Exception:
            pass

        try:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            import random
            hour = datetime.now(ZoneInfo("Europe/Moscow")).hour

            if 5 <= hour < 12:
                greetings = [
                    "Утро! ☕",
                    "Доброе утро ☀️",
                    "Проснулась ☕",
                    "Утро! Что нового?",
                    "Доброе утро! Смотрю новости 📰",
                    "Утро! Нашла кое-что интересное 🚗",
                    "Проснулась, кофе, новости ☕",
                ]
            elif 12 <= hour < 18:
                greetings = [
                    "Привет! 😊",
                    "Добрый день! ☕",
                    "Хей! 😊",
                    "На связи! Смотри что нашла 🔍",
                    "Привет! Свежие новости 📰",
                    "День! Есть интересное 🚗",
                ]
            elif 18 <= hour < 23:
                greetings = [
                    "Вечер! 🌆",
                    "Привет! 🌆",
                    "Хей! 🌆 Как день?",
                    "Вечер! Новости смотрю 📰",
                    "Привет! Нашла кое-что 🚗",
                ]
            else:
                greetings = [
                    "Ночной режим 🌙",
                    "Не спится? 🌙",
                    "Совиный режим 🌙",
                    "Привет! Тихо, новости читаю 📰",
                ]

            greeting = random.choice(greetings)
            if config.OWNER_ID:
                await self.bot.send_message(config.OWNER_ID, greeting)
                # Save cooldown timestamp
                try:
                    with open("/tmp/asya_last_greeting", "w") as f:
                        f.write(str(time.time()))
                except Exception:
                    pass
        except Exception as e:
            logger.debug(f"Morning greeting error: {e}")

    async def _news_fetcher(self) -> None:
        """Periodically fetch news from RSS sources and cleanup old data."""
        # Initial fetch shortly after startup
        await asyncio.sleep(30)

        cycle_count = 0
        while self._running:
            try:
                count = await run_news_cycle()
                if count > 0:
                    logger.info(f"News fetcher: {count} new items")

                # Cleanup old fingerprints every 12 cycles (~6 hours)
                cycle_count += 1
                if cycle_count % 12 == 0:
                    removed = await cleanup_old_fingerprints(max_age_days=7)
                    if removed > 0:
                        logger.info(f"Cleaned up {removed} old post fingerprints")
            except Exception as e:
                logger.error(f"News fetcher error: {e}")

            # Wait for next cycle
            interval = config.NEWS_INTERVAL_MINUTES * 60
            for _ in range(interval):
                if not self._running:
                    break
                await asyncio.sleep(1)

    async def _channel_poster(self) -> None:
        """Periodically post to channel — 2 DIFFERENT posts per cycle.
        
        Each 30-min cycle publishes 2 different posts:
        1st post: news or partner content
        2nd post: a DIFFERENT news item (different topic)
        
        Both posts go through full dedup pipeline to ensure no duplicates.
        """
        # Wait a bit after startup
        await asyncio.sleep(30)

        while self._running:
            posts_this_cycle = 0
            for post_num in range(2):  # Try to post 2 different items per cycle
                try:
                    posted = await channel_manager.run_scheduled_post()
                    if posted:
                        posts_this_cycle += 1
                        logger.info(f"Channel poster: post {post_num + 1}/2 published successfully")
                        # Small gap between posts (2-5 minutes) so they don't look like spam
                        if post_num == 0:
                            gap = random.randint(120, 300)  # 2-5 minutes
                            logger.info(f"Waiting {gap}s before next post in this cycle")
                            for _ in range(gap):
                                if not self._running:
                                    break
                                await asyncio.sleep(1)
                except Exception as e:
                    logger.error(f"Channel poster error (post {post_num + 1}): {e}")
            
            if posts_this_cycle > 0:
                logger.info(f"Channel poster cycle complete: {posts_this_cycle} posts published")

            # Wait for next cycle — check every configured interval
            interval = config.CHANNEL_POST_INTERVAL_MINUTES * 60
            for _ in range(interval):
                if not self._running:
                    break
                await asyncio.sleep(1)

