"""
Mini App Server — Web server for Ася Telegram Mini App.
Serves the web app HTML and provides API endpoints for:
- Chat with Asya
- VIN decoding
- Car diagnostics
- Spare part search

Runs alongside the main bot (aiogram) using httpx + asyncio.
No aiohttp dependency!
"""

import logging
import json
import os
import asyncio
from typing import Optional

from ai.router import ai_router
from bot.config import config

logger = logging.getLogger("asya.miniapp")

# Path to miniapp static files
MINIAPP_DIR = os.path.dirname(os.path.abspath(__file__))


class MiniAppServer:
    """Simple HTTP server using asyncio + manual HTTP parsing.
    Replaces aiohttp — no external web framework needed.
    The Mini App is primarily served via GitHub Pages anyway.
    """

    def __init__(self, host: str = "0.0.0.0", port: int = 8080):
        self.host = host
        self.port = port
        self._server: Optional[asyncio.AbstractServer] = None

    async def start(self):
        """Start the HTTP server."""
        self._server = await asyncio.start_server(
            self._handle_request, self.host, self.port
        )
        logger.info(f"Mini App server started on {self.host}:{self.port}")

    async def stop(self):
        """Stop the HTTP server."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _handle_request(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Handle incoming HTTP request."""
        try:
            # Read request line and headers
            request_line = await reader.readline()
            if not request_line:
                writer.close()
                return

            request_str = request_line.decode("utf-8", errors="ignore").strip()
            headers = {}
            body = b""

            # Read headers
            while True:
                line = await reader.readline()
                if not line or line == b"\r\n":
                    break
                line_str = line.decode("utf-8", errors="ignore").strip()
                if ":" in line_str:
                    key, val = line_str.split(":", 1)
                    headers[key.strip().lower()] = val.strip()

            # Read body if Content-Length present
            content_length = int(headers.get("content-length", 0))
            if content_length > 0:
                body = await reader.readexactly(content_length)

            # Parse request
            parts = request_str.split(" ")
            method = parts[0] if len(parts) > 0 else "GET"
            path = parts[1] if len(parts) > 1 else "/"

            # Route request
            if method == "GET" and path in ("/", "/app"):
                await self._serve_html(writer)
            elif method == "GET" and path == "/api/health":
                await self._send_json(writer, 200, {"status": "ok", "bot": "asya", "version": "2.0"})
            elif method == "POST" and path == "/api/chat":
                await self._handle_chat(writer, body)
            elif method == "POST" and path == "/api/vin":
                await self._handle_vin(writer, body)
            elif method == "POST" and path == "/api/diagnostic":
                await self._handle_diagnostic(writer, body)
            elif method == "POST" and path == "/api/parts":
                await self._handle_parts(writer, body)
            else:
                await self._send_json(writer, 404, {"error": "Not found"})

        except Exception as e:
            logger.error(f"MiniApp request error: {e}")
            try:
                await self._send_json(writer, 500, {"error": str(e)})
            except Exception:
                pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _serve_html(self, writer: asyncio.StreamWriter):
        """Serve the Mini App HTML page."""
        html_path = os.path.join(MINIAPP_DIR, "index.html")
        try:
            with open(html_path, "r", encoding="utf-8") as f:
                html = f.read()
            await self._send_response(writer, 200, html, content_type="text/html; charset=utf-8")
        except FileNotFoundError:
            await self._send_json(writer, 404, {"error": "Mini App HTML not found"})

    async def _handle_chat(self, writer: asyncio.StreamWriter, body: bytes):
        """API: Chat with Asya."""
        try:
            data = json.loads(body)
            message = data.get("message", "").strip()
            user_id = data.get("user_id", 0)

            if not message:
                await self._send_json(writer, 400, {"text": "Пустое сообщение!"})
                return

            response = await ai_router.chat(
                user_id=user_id,
                message=message,
                use_cache=True,
                save_history=True,
            )

            if response.error or not response.text:
                await self._send_json(writer, 500, {"text": "Не удалось получить ответ. Попробуйте ещё раз."})
                return

            await self._send_json(writer, 200, {"text": response.text})

        except Exception as e:
            logger.error(f"MiniApp chat error: {e}")
            await self._send_json(writer, 500, {"text": f"Ошибка: {e}"})

    async def _handle_vin(self, writer: asyncio.StreamWriter, body: bytes):
        """API: Decode VIN code."""
        try:
            data = json.loads(body)
            vin = data.get("vin", "").strip().upper()
            user_id = data.get("user_id", 0)

            if not vin:
                await self._send_json(writer, 400, {"text": "Укажите VIN-код!"})
                return

            response = await ai_router.decode_vin(
                user_id=user_id,
                vin_code=vin,
            )

            if response.error or not response.text:
                await self._send_json(writer, 500, {"text": "Не удалось расшифровать VIN. Попробуйте ещё раз."})
                return

            await self._send_json(writer, 200, {"text": response.text})

        except Exception as e:
            logger.error(f"MiniApp VIN error: {e}")
            await self._send_json(writer, 500, {"text": f"Ошибка: {e}"})

    async def _handle_diagnostic(self, writer: asyncio.StreamWriter, body: bytes):
        """API: Car diagnostics."""
        try:
            data = json.loads(body)
            symptoms = data.get("symptoms", "").strip()
            user_id = data.get("user_id", 0)

            if not symptoms:
                await self._send_json(writer, 400, {"text": "Опишите симптомы!"})
                return

            response = await ai_router.diagnose_car(
                user_id=user_id,
                symptoms=symptoms,
            )

            if response.error or not response.text:
                await self._send_json(writer, 500, {"text": "Не удалось диагностировать. Попробуйте ещё раз."})
                return

            await self._send_json(writer, 200, {"text": response.text})

        except Exception as e:
            logger.error(f"MiniApp diagnostic error: {e}")
            await self._send_json(writer, 500, {"text": f"Ошибка: {e}"})

    async def _handle_parts(self, writer: asyncio.StreamWriter, body: bytes):
        """API: Spare part search."""
        try:
            data = json.loads(body)
            article = data.get("article", "").strip()
            user_id = data.get("user_id", 0)

            if not article:
                await self._send_json(writer, 400, {"text": "Укажите артикул запчасти!"})
                return

            response = await ai_router.find_spare_part(
                user_id=user_id,
                article=article,
            )

            if response.error or not response.text:
                await self._send_json(writer, 500, {"text": "Не удалось найти запчасть. Попробуйте ещё раз."})
                return

            await self._send_json(writer, 200, {"text": response.text})

        except Exception as e:
            logger.error(f"MiniApp parts error: {e}")
            await self._send_json(writer, 500, {"text": f"Ошибка: {e}"})

    async def _send_json(self, writer: asyncio.StreamWriter, status: int, data: dict):
        """Send a JSON response."""
        body = json.dumps(data, ensure_ascii=False)
        await self._send_response(writer, status, body, content_type="application/json; charset=utf-8")

    async def _send_response(self, writer: asyncio.StreamWriter, status: int, body: str,
                              content_type: str = "text/plain; charset=utf-8"):
        """Send an HTTP response."""
        status_messages = {200: "OK", 400: "Bad Request", 404: "Not Found", 500: "Internal Server Error"}
        status_msg = status_messages.get(status, "OK")
        body_bytes = body.encode("utf-8")

        response = (
            f"HTTP/1.1 {status} {status_msg}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body_bytes)}\r\n"
            f"Access-Control-Allow-Origin: *\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        ).encode("utf-8") + body_bytes

        writer.write(response)
        await writer.drain()


async def start_miniapp_server(host: str = "0.0.0.0", port: int = 8080):
    """Start the Mini App web server (replaces aiohttp — no external dependency!)."""
    server = MiniAppServer(host, port)
    await server.start()
    return server
