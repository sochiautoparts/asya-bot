"""
Mini App Server — Web server for Ася Telegram Mini App.
Serves the web app HTML and provides API endpoints for:
- Chat with Asya
- VIN decoding
- Car diagnostics
- Spare part search

Runs alongside the main bot (aiogram) using aiohttp.
"""

import logging
import json
import os
from aiohttp import web

from ai.router import ai_router
from bot.config import config

logger = logging.getLogger("asya.miniapp")

# Path to miniapp static files
MINIAPP_DIR = os.path.dirname(os.path.abspath(__file__))


async def handle_index(request: web.Request) -> web.Response:
    """Serve the Mini App HTML page."""
    html_path = os.path.join(MINIAPP_DIR, "index.html")
    return web.FileResponse(html_path)


async def handle_chat(request: web.Request) -> web.Response:
    """API: Chat with Asya."""
    try:
        data = await request.json()
        message = data.get("message", "").strip()
        user_id = data.get("user_id", 0)

        if not message:
            return web.json_response({"text": "Пустое сообщение!"}, status=400)

        response = await ai_router.chat(
            user_id=user_id,
            message=message,
            use_cache=True,
            save_history=True,
        )

        if response.error or not response.text:
            return web.json_response({"text": "Не удалось получить ответ. Попробуйте ещё раз."}, status=500)

        return web.json_response({"text": response.text})

    except Exception as e:
        logger.error(f"MiniApp chat error: {e}")
        return web.json_response({"text": f"Ошибка: {e}"}, status=500)


async def handle_vin(request: web.Request) -> web.Response:
    """API: Decode VIN code."""
    try:
        data = await request.json()
        vin = data.get("vin", "").strip().upper()
        user_id = data.get("user_id", 0)

        if not vin:
            return web.json_response({"text": "Укажите VIN-код!"}, status=400)

        response = await ai_router.decode_vin(
            user_id=user_id,
            vin_code=vin,
        )

        if response.error or not response.text:
            return web.json_response({"text": "Не удалось расшифровать VIN. Попробуйте ещё раз."}, status=500)

        return web.json_response({"text": response.text})

    except Exception as e:
        logger.error(f"MiniApp VIN error: {e}")
        return web.json_response({"text": f"Ошибка: {e}"}, status=500)


async def handle_diagnostic(request: web.Request) -> web.Response:
    """API: Car diagnostics."""
    try:
        data = await request.json()
        symptoms = data.get("symptoms", "").strip()
        user_id = data.get("user_id", 0)

        if not symptoms:
            return web.json_response({"text": "Опишите симптомы!"}, status=400)

        response = await ai_router.diagnose_car(
            user_id=user_id,
            symptoms=symptoms,
        )

        if response.error or not response.text:
            return web.json_response({"text": "Не удалось диагностировать. Попробуйте ещё раз."}, status=500)

        return web.json_response({"text": response.text})

    except Exception as e:
        logger.error(f"MiniApp diagnostic error: {e}")
        return web.json_response({"text": f"Ошибка: {e}"}, status=500)


async def handle_parts(request: web.Request) -> web.Response:
    """API: Spare part search."""
    try:
        data = await request.json()
        article = data.get("article", "").strip()
        user_id = data.get("user_id", 0)

        if not article:
            return web.json_response({"text": "Укажите артикул запчасти!"}, status=400)

        response = await ai_router.find_spare_part(
            user_id=user_id,
            article=article,
        )

        if response.error or not response.text:
            return web.json_response({"text": "Не удалось найти запчасть. Попробуйте ещё раз."}, status=500)

        return web.json_response({"text": response.text})

    except Exception as e:
        logger.error(f"MiniApp parts error: {e}")
        return web.json_response({"text": f"Ошибка: {e}"}, status=500)


async def handle_health(request: web.Request) -> web.Response:
    """Health check endpoint."""
    return web.json_response({"status": "ok", "bot": "asya", "version": "1.0"})


def create_miniapp_app() -> web.Application:
    """Create the aiohttp application for the Mini App server."""
    app = web.Application()

    # Static routes
    app.router.add_get("/", handle_index)
    app.router.add_get("/app", handle_index)

    # API routes
    app.router.add_post("/api/chat", handle_chat)
    app.router.add_post("/api/vin", handle_vin)
    app.router.add_post("/api/diagnostic", handle_diagnostic)
    app.router.add_post("/api/parts", handle_parts)
    app.router.add_get("/api/health", handle_health)

    logger.info("Mini App server routes configured")
    return app


async def start_miniapp_server(host: str = "0.0.0.0", port: int = 8080):
    """Start the Mini App web server."""
    app = create_miniapp_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info(f"Mini App server started on {host}:{port}")
    return runner
