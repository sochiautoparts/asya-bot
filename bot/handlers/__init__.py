"""
Handler Router Aggregation — Combines all handler routers.
"""

from aiogram import Router

from bot.handlers.chat import chat_router
from bot.handlers.admin import admin_router
from bot.handlers.inline import inline_router


def get_all_routers() -> Router:
    """Aggregate all handler routers into one main router."""
    main_router = Router()
    main_router.include_router(chat_router)
    main_router.include_router(admin_router)
    main_router.include_router(inline_router)
    return main_router
