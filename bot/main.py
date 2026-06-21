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
import faulthandler
import logging
import os
import random
import signal
import sys
import time
import fcntl
from pathlib import Path

# Enable faulthandler for C-level crash diagnostics (segfaults in llama-cpp)
faulthandler.enable()

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from bot.config import config
from bot.database import (
    init_db, cleanup_old_fingerprints, add_chat_message, load_topic_registry,
    run_periodic_cleanup,
)
from bot.partners import partner_manager
from ai.router import ai_router
from news import run_news_cycle
from channel import channel_manager

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
            asyncio.create_task(self._shop_poster(), name="shop_poster"),
            asyncio.create_task(self._shop_refresher(), name="shop_refresher"),
        ]
        logger.info("Background tasks started (news + posts + shop selections + shop refresh)")

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
                # Save greeting to chat history so AI knows what it said
                # when the user replies — prevents context loss
                try:
                    await add_chat_message(config.OWNER_ID, "assistant", greeting)
                except Exception as e:
                    logger.debug(f"Could not save greeting to chat history: {e}")
                # Save cooldown timestamp
                try:
                    with open("/tmp/asya_last_greeting", "w") as f:
                        f.write(str(time.time()))
                except Exception:
                    pass
        except Exception as e:
            logger.debug(f"Morning greeting error: {e}")

    async def _news_fetcher(self) -> None:
        """Periodically fetch news from sochiautoparts/nws (single source) and cleanup old data."""
        # Initial fetch shortly after startup
        await asyncio.sleep(30)

        cycle_count = 0
        while self._running:
            try:
                count = await run_news_cycle()
                if count > 0:
                    logger.info(f"News fetcher: {count} new items")

                # Cleanup old fingerprints every 12 cycles (~6 hours with 30min interval)
                cycle_count += 1
                if cycle_count % 12 == 0:
                    removed = await cleanup_old_fingerprints(max_age_days=7)
                    if removed > 0:
                        logger.info(f"Cleaned up {removed} old post fingerprints")

                # Auto-refresh partner data every 6 hours (every 12 cycles)
                if cycle_count % 12 == 0:
                    try:
                        from bot.partners import partner_manager
                        await partner_manager.maybe_refresh()
                    except Exception as e:
                        logger.debug(f"Partner data refresh skipped: {e}")

                # v5.1: Run full periodic DB cleanup every 24 cycles (~12 hours).
                # Cleans up chat_history (>30d), ai_cache (>7d), news_items (>7d posted),
                # posted_urls (>30d), partner_posts (>60d), channel_posts (>90d).
                # Keeps the DB small and queries fast over time.
                if cycle_count % 24 == 0:
                    try:
                        cleanup_results = await run_periodic_cleanup()
                        total_removed = sum(cleanup_results.values())
                        if total_removed > 0:
                            logger.info(f"Periodic DB cleanup: removed {total_removed} rows total")
                    except Exception as e:
                        logger.warning(f"Periodic DB cleanup failed: {e}")
            except Exception as e:
                logger.error(f"News fetcher error: {e}")

            # Wait for next cycle
            interval = config.NEWS_INTERVAL_MINUTES * 60
            for _ in range(interval):
                if not self._running:
                    break
                await asyncio.sleep(1)

    async def _channel_poster(self) -> None:
        """Post to channel periodically — like nastya-bot continuous posting.
        
        Schedule:
        - Every CHANNEL_POST_INTERVAL_MINUTES (default 20 min)
        - 1 partner post + 1-2 news posts per cycle
        - 3-5 second gap between posts to avoid Telegram rate limits
        - All posts go through full dedup pipeline to ensure no duplicates
        
        This matches nastya-bot's posting pattern: frequent small batches
        instead of one big hourly batch.
        """
        # Wait a bit after startup (like nastya-bot waits 120s)
        await asyncio.sleep(120)
        
        interval_seconds = config.CHANNEL_POST_INTERVAL_MINUTES * 60
        logger.info(f"Channel poster started — interval {config.CHANNEL_POST_INTERVAL_MINUTES}min, "
                     f"posting continuously like nastya-bot")

        consecutive_empty_cycles = 0

        while self._running:
            posts_this_cycle = 0
            
            # ── 1. Try partner post (not every cycle — every 3rd) ──
            if consecutive_empty_cycles % 3 == 0:
                try:
                    posted = await channel_manager.post_partner_content()
                    if posted:
                        posts_this_cycle += 1
                        logger.info("Channel poster: partner post published")
                        # Brief gap
                        for _ in range(5):
                            if not self._running:
                                break
                            await asyncio.sleep(1)
                except Exception as e:
                    logger.error(f"Channel poster partner error: {e}", exc_info=True)
            
            # ── 2. News post — 1-2 per cycle (like nastya-bot does 3) ──
            max_news = min(2, config.CHANNEL_NEWS_PER_HOUR)
            news_posted = 0
            for i in range(max_news):
                if not self._running:
                    break
                    
                try:
                    posted = await channel_manager.post_news()
                    if posted:
                        news_posted += 1
                        posts_this_cycle += 1
                        logger.info(f"Channel poster: news post {news_posted}/{max_news} published")
                        # Gap between posts (3-5 seconds)
                        gap = random.randint(3, 5)
                        for _ in range(gap):
                            if not self._running:
                                break
                            await asyncio.sleep(1)
                    else:
                        logger.info(f"Channel poster: news post {i+1} not posted (dedup, AI failure, or no fresh content)")
                        continue
                except Exception as e:
                    logger.error(f"Channel poster news error (post {i+1}): {e}", exc_info=True)
                    break
            
            if posts_this_cycle > 0:
                logger.info(f"Channel poster: cycle complete — {posts_this_cycle} posts ({news_posted} news + partner)")
                consecutive_empty_cycles = 0
            else:
                consecutive_empty_cycles += 1
                logger.warning(f"Channel poster: no posts this cycle ({consecutive_empty_cycles} consecutive)")
                # Health check: alert owner after 6 consecutive empty cycles (~2h)
                if consecutive_empty_cycles >= 6 and self.bot:
                    try:
                        await self.bot.send_message(
                            chat_id=config.OWNER_ID,
                            text=f"⚠️ Ася: {consecutive_empty_cycles} циклов подряд без постов. Проверь логи."
                        )
                    except Exception:
                        pass

            # Wait for next cycle — like nastya-bot's CHANNEL_POST_INTERVAL
            logger.info(f"Channel poster: waiting {config.CHANNEL_POST_INTERVAL_MINUTES} minutes until next cycle")
            for _ in range(interval_seconds):
                if not self._running:
                    break
                await asyncio.sleep(1)

    # ── Shop selection poster — 4 times per day at fixed Moscow times ──
    # Schedule (Europe/Moscow):
    #   09:00, 13:00, 17:00, 21:00
    # Bot may restart every ~5h (GitHub Actions workflow), so we check every 5 min
    # and fire if we're within 30 min of a slot AND haven't posted that slot today.
    # Uses /tmp file markers keyed by date+slot to dedup across restarts.

    SHOP_SELECTION_SLOTS = [
        (9, 0),    # 09:00 Moscow
        (13, 0),   # 13:00 Moscow
        (17, 0),   # 17:00 Moscow
        (21, 0),   # 21:00 Moscow
    ]
    SHOP_SLOT_WINDOW_MIN = 30  # Fire if within ±30 min of slot time
    SHOP_CHECK_INTERVAL = 300  # Check every 5 minutes

    async def _shop_poster(self) -> None:
        """Post product selections 4 times per day at fixed Moscow times.

        Strategy:
          - Loop forever, checking every 5 min if we're near a scheduled slot
          - For each slot, mark a /tmp file when fired so we don't fire twice
            (even across bot restarts within the same day)
          - On startup, fire any missed slot from the last 2 hours (catch-up)
        """
        await asyncio.sleep(60)  # Wait for other systems to warm up
        logger.info(
            f"Shop poster started — 4 selections/day at "
            f"{', '.join(f'{h:02d}:{m:02d}' for h, m in self.SHOP_SELECTION_SLOTS)} Moscow"
        )

        while self._running:
            try:
                await self._check_and_post_shop_selection()
            except Exception as e:
                logger.error(f"Shop poster error: {e}", exc_info=True)

            # Wait until next check
            for _ in range(self.SHOP_CHECK_INTERVAL):
                if not self._running:
                    break
                await asyncio.sleep(1)

    async def _check_and_post_shop_selection(self) -> None:
        """Check if we should fire a shop selection now, and fire if so."""
        import os
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from bot.database import get_last_shop_selection_time
        from channel import channel_manager

        now_msk = datetime.now(ZoneInfo("Europe/Moscow"))
        now_minutes = now_msk.hour * 60 + now_msk.minute
        today_str = now_msk.strftime("%Y-%m-%d")

        # Find the closest slot we should fire
        slot_to_fire = None
        for slot_idx, (h, m) in enumerate(self.SHOP_SELECTION_SLOTS):
            slot_minutes = h * 60 + m
            diff = now_minutes - slot_minutes
            # Fire if we're 0..30 min AFTER the slot time (don't fire BEFORE)
            if 0 <= diff <= self.SHOP_SLOT_WINDOW_MIN:
                marker_file = f"/tmp/asya_shop_slot_{today_str}_{slot_idx}"
                if os.path.exists(marker_file):
                    # Already fired this slot today
                    continue
                slot_to_fire = (slot_idx, marker_file)
                break

        if slot_to_fire is None:
            return

        slot_idx, marker_file = slot_to_fire

        # ── Catch-up: if the DB shows no selection in last 2h, fire even if we're
        # past the 30-min window (e.g. bot was offline at slot time) ──
        # We already check above; if we're here, we're within the window.

        # Don't fire if a selection was posted in the last 90 min (avoid bursts
        # when the 30-min window overlaps with a recent post)
        last_post_time = await get_last_shop_selection_time()
        if last_post_time > 0:
            import time as _time
            elapsed = _time.time() - last_post_time
            if elapsed < 5400:  # 90 min
                logger.debug(
                    f"Shop selection: last post was {int(elapsed/60)} min ago, "
                    f"skipping slot {slot_idx} (too soon)"
                )
                # Mark the slot as fired so we don't keep retrying
                try:
                    with open(marker_file, "w") as f:
                        f.write(str(_time.time()))
                except Exception:
                    pass
                return

        # Mark BEFORE firing (so a crash mid-fire doesn't cause a retry storm)
        import time as _time
        try:
            with open(marker_file, "w") as f:
                f.write(str(_time.time()))
        except Exception:
            pass

        # Make sure we have at least some products in the DB
        from bot.database import get_shop_stats
        stats = await get_shop_stats()
        if stats["total_products"] < 10:
            logger.warning(
                f"Shop selection: only {stats['total_products']} products in DB, "
                f"refreshing catalog first..."
            )
            try:
                from bot.shop import refresh_all_categories_light
                await refresh_all_categories_light(max_per_category=8)
            except Exception as e:
                logger.error(f"Shop catalog refresh failed: {e}")

        # Fire!
        logger.info(f"Shop selection slot {slot_idx} firing (Moscow {now_msk.strftime('%H:%M')})")
        try:
            posted = await channel_manager.post_product_selection(
                category_label=None,  # Random category
                count=5,
                trigger_reason="scheduled",
            )
            if posted:
                logger.info(f"Shop selection slot {slot_idx} posted successfully")
            else:
                logger.warning(f"Shop selection slot {slot_idx} did not post (no products?)")
                # Don't unmark — try again next slot
        except Exception as e:
            logger.error(f"Shop selection slot {slot_idx} failed: {e}", exc_info=True)

    async def _shop_refresher(self) -> None:
        """Periodically refresh the shop catalog.

        Schedule:
          - Initial refresh 5 min after startup (if DB is empty or stale)
          - Then every 6 hours, refresh one random category (rotating)
          - Every 24 cycles (~6 days), cleanup old products
        """
        await asyncio.sleep(300)  # 5 min after startup

        from bot.shop import refresh_random_category, refresh_all_categories_light
        from bot.database import get_shop_stats, cleanup_old_shop_products

        # Initial check: if DB has fewer than 50 products, do a light refresh of all categories
        try:
            stats = await get_shop_stats()
            if stats["total_products"] < 50:
                logger.info(
                    f"Shop refresher: DB has only {stats['total_products']} products, "
                    f"doing initial light refresh of all categories"
                )
                try:
                    results = await refresh_all_categories_light(max_per_category=8)
                    total_new = sum(results.values())
                    logger.info(
                        f"Shop refresher: initial refresh done — {total_new} new products "
                        f"across {len(results)} categories"
                    )
                except Exception as e:
                    logger.error(f"Shop refresher: initial refresh failed: {e}")
        except Exception as e:
            logger.warning(f"Shop refresher: initial check failed: {e}")

        cycle_count = 0
        while self._running:
            cycle_count += 1
            try:
                slug, new_count = await refresh_random_category()
                logger.info(f"Shop refresher: cycle {cycle_count} refreshed '{slug}' (+{new_count} new)")
            except Exception as e:
                logger.warning(f"Shop refresher: cycle {cycle_count} failed: {e}")

            # Every 24 cycles (~6 days at 6h interval), cleanup old products
            if cycle_count % 24 == 0:
                try:
                    removed = await cleanup_old_shop_products(max_age_days=14)
                    if removed > 0:
                        logger.info(f"Shop refresher: cleaned up {removed} stale products")
                except Exception as e:
                    logger.debug(f"Shop refresher: cleanup failed: {e}")

            # Wait 6 hours
            interval = 6 * 3600
            for _ in range(interval):
                if not self._running:
                    break
                await asyncio.sleep(1)


# ── Main Entry Point ──────────────────────────────────────────────────────────

async def main():
    """Main entry point for Asya Bot."""
    # Check bot token
    if not config.BOT_TOKEN:
        logger.critical("BOT_TOKEN not set! Exiting.")
        sys.exit(1)

    # Acquire singleton lock
    lock = SingletonLock(config.LOCK_FILE)
    if not lock.acquire():
        logger.warning("Another instance is running, exiting.")
        sys.exit(0)

    # Create bot
    bot = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    # Delete webhook to ensure polling works
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Webhook deleted, polling mode ready")
    except Exception as e:
        logger.warning(f"Could not delete webhook: {e}")

    # Initialize database
    await init_db()
    logger.info("Database initialized")

    # Load topic registry from DB — CRITICAL for dedup after restart
    # Without this, the bot re-posts the same topics after every restart
    try:
        from bot.content_engine import _topic_registry, _register_topic, _is_topic_covered
        loaded_registry = await load_topic_registry()
        if loaded_registry:
            # Import the content_engine module to set its global registry
            import bot.content_engine as ce
            ce._topic_registry = loaded_registry
            logger.info(f"Topic registry loaded: {len(loaded_registry)} topics from DB (dedup active)")
        else:
            logger.info("Topic registry empty — first run or all topics expired")
    except Exception as e:
        logger.warning(f"Could not load topic registry from DB: {e}")

    # Initialize AI router
    await ai_router.initialize()
    logger.info("AI Router initialized")

    # Load partner programs
    try:
        partner_count = await partner_manager.load_async()
        logger.info(f"Partner programs loaded: {partner_count}")
    except Exception as e:
        logger.warning(f"Could not load partner programs: {e}")

    # v5.1: Preload all partner logos in parallel (network + SVG→PNG conversion).
    # Results are cached on disk (data/partner_logos/) so subsequent restarts
    # are instant. First partner channel post won't have to wait for download.
    try:
        logo_count = await partner_manager.preload_all_logos()
        logger.info(f"Partner logos preloaded: {logo_count}/{partner_count}")
    except Exception as e:
        logger.debug(f"Partner logo preload skipped: {e}")

    # Set bot on channel manager
    channel_manager.set_bot(bot)

    # Load recently posted titles into semantic dedup (prevents duplicates after restart)
    try:
        await channel_manager.load_recent_semantic_data()
    except Exception as e:
        logger.warning(f"Could not load semantic dedup data: {e}")

    # Set up dispatcher
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)

    # Include all handler routers — include EACH sub-router individually
    # to avoid "Router object is not iterable" error in some aiogram versions
    #
    # CRITICAL: admin_router MUST be registered BEFORE chat_router.
    # In aiogram 3.x routers are checked in registration order. chat_router
    # has a catch-all `@chat_router.message(F.text)` handler that would
    # swallow admin commands (/shop_status, /selection, /shop_refresh,
    # /admin, /post, /partner_post, /news, /search, /status, /models, /switch,
    # /reload_partners) and route them to the AI chat handler — which then
    # generates a generic AI response instead of executing the command.
    # Putting admin_router first ensures Command() filters run before F.text.
    try:
        from bot.handlers.chat import chat_router
        from bot.handlers.admin import admin_router
        from bot.handlers.inline import inline_router
        dp.include_router(admin_router)   # ← FIRST (Command() filters)
        dp.include_router(chat_router)    # ← SECOND (F.text catch-all)
        dp.include_router(inline_router)  # ← THIRD (inline queries)
        logger.info("Handler routers included (admin → chat → inline)")
    except Exception as e:
        logger.critical(f"Failed to include handler routers: {e}")
        raise

    # ── Attach Guest Mode middleware (Bot API 10.0 — May 2026) ──
    # Lets the bot receive and reply to messages in chats it is NOT a member of.
    # Implemented via raw HTTP because aiogram 3.15 doesn't natively support
    # the new guest_message field on Update.
    try:
        from bot.guest_mode import attach_guest_mode
        attach_guest_mode(dp, bot)
    except Exception as e:
        logger.warning(f"Guest Mode attachment failed (non-fatal): {e}")

    # Start background tasks
    bg_tasks = BackgroundTasks(bot)

    async def on_startup():
        """Startup callback — start background tasks."""
        await bg_tasks.start()

    async def on_shutdown():
        """Shutdown callback — stop background tasks."""
        await bg_tasks.stop()

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    # Run polling
    logger.info("=== Asya Bot Starting (Local-First v12.2 — RuadaptQwen3-4B) ===")
    local_status = "enabled" if config.ENABLE_LOCAL_MODEL else "disabled"
    model_info = f", model={config.MODEL_PATH}" if config.ENABLE_LOCAL_MODEL and config.MODEL_PATH else ""
    logger.info(f"Local model: {local_status}{model_info}, Cloud: Pollinations + Cloudflare")
    try:
        await dp.start_polling(bot)
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    finally:
        await bg_tasks.stop()
        lock.release()
        try:
            await bot.session.close()
        except Exception:
            pass
        logger.info("=== Asya Bot Stopped ===")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
        sys.exit(code)
    except Exception as e:
        logger.critical(f"Fatal error: {e}")
        sys.exit(1)
