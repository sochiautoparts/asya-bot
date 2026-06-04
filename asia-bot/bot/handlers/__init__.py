from aiogram import Router
from .chat import router as chat_router
from .inline import router as inline_router

all_routers = [chat_router, inline_router]
