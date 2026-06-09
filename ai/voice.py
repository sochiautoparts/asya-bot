"""
Voice Transcription — Downloads voice messages and transcribes them
using Pollinations AI (with key → free API fallback).
"""

import os
import tempfile
import logging
from typing import Optional

import httpx

from bot.config import config

logger = logging.getLogger("asya.voice")


async def download_voice_file(bot, file_id: str) -> Optional[str]:
    """
    Download a voice message from Telegram and return the file path.
    Returns None on failure.
    """
    try:
        file_info = await bot.get_file(file_id)
        if not file_info or not file_info.file_path:
            logger.warning(f"Could not get file info for {file_id}")
            return None

        file_url = f"https://api.telegram.org/file/bot{config.BOT_TOKEN}/{file_info.file_path}"

        # Determine extension
        ext = ".ogg"  # Telegram voice messages are OGG Opus
        if file_info.file_path.endswith(".mp3"):
            ext = ".mp3"
        elif file_info.file_path.endswith(".wav"):
            ext = ".wav"

        # Download to temp file
        tmp_dir = tempfile.gettempdir()
        tmp_path = os.path.join(tmp_dir, f"asya_voice_{file_id}{ext}")

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(file_url)
            if response.status_code == 200:
                with open(tmp_path, "wb") as f:
                    f.write(response.content)
                logger.info(f"Downloaded voice to {tmp_path} ({len(response.content)} bytes)")
                return tmp_path
            else:
                logger.error(f"Failed to download voice: HTTP {response.status_code}")
                return None

    except Exception as e:
        logger.error(f"Error downloading voice: {e}")
        return None


async def transcribe_voice(file_path: str, language: str = "ru") -> Optional[str]:
    """
    Transcribe a voice message using Pollinations AI transcription.
    Falls back to free API (no auth) when keys are depleted.

    Chain: Pollinations (key) → Pollinations (free) → None
    """
    if not file_path or not os.path.exists(file_path):
        logger.error(f"Voice file not found: {file_path}")
        return None

    # ── LEVEL 1: Pollinations with API key ──
    api_key = config.POLLINATIONS_API_KEY
    if api_key:
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                with open(file_path, "rb") as audio_file:
                    files = {"file": (os.path.basename(file_path), audio_file, "audio/ogg")}
                    data = {"language": language}

                    headers = {
                        "Authorization": f"Bearer {api_key}",
                    }

                    response = await client.post(
                        f"{config.POLLINATIONS_BASE_URL}/v1/audio/transcriptions",
                        files=files,
                        data=data,
                        headers=headers,
                    )

                    if response.status_code == 200:
                        result = response.json()
                        text = result.get("text", "")
                        if text:
                            logger.info(f"Transcribed voice (key): {text[:100]}")
                            return text
                    elif response.status_code in (401, 402):
                        logger.warning(f"Voice transcription key error: {response.status_code}, trying fallback...")
                    else:
                        logger.warning(f"Transcription API returned {response.status_code}: {response.text[:200]}")

        except Exception as e:
            logger.error(f"Error transcribing voice (key): {e}")

    # ── LEVEL 2: Try second key ──
    api_key_2 = config.POLLINATIONS_API_KEY_2
    if api_key_2:
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                with open(file_path, "rb") as audio_file:
                    files = {"file": (os.path.basename(file_path), audio_file, "audio/ogg")}
                    data = {"language": language}

                    response = await client.post(
                        f"{config.POLLINATIONS_BASE_URL}/v1/audio/transcriptions",
                        files=files,
                        data=data,
                        headers={"Authorization": f"Bearer {api_key_2}"},
                    )

                    if response.status_code == 200:
                        result = response.json()
                        text = result.get("text", "")
                        if text:
                            logger.info(f"Transcribed voice (key2): {text[:100]}")
                            return text
                    elif response.status_code in (401, 402):
                        logger.warning(f"Voice transcription key2 error: {response.status_code}, trying free API...")
                    else:
                        logger.warning(f"Transcription API (key2) returned {response.status_code}")

        except Exception as e:
            logger.error(f"Error transcribing voice (key2): {e}")

    # ── LEVEL 3: Free Pollinations API (no auth) ──
    free_url = config.POLLINATIONS_FREE_TEXT_URL
    if free_url:
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                with open(file_path, "rb") as audio_file:
                    # Try free endpoint — NO Authorization header
                    files = {"file": (os.path.basename(file_path), audio_file, "audio/ogg")}
                    data = {"language": language}
                    # No auth header for free API

                    response = await client.post(
                        f"{free_url}/openai/audio/transcriptions",
                        files=files,
                        data=data,
                    )

                    if response.status_code == 200:
                        result = response.json()
                        text = result.get("text", "")
                        if text:
                            logger.info(f"Transcribed voice (free): {text[:100]}")
                            return text
                    else:
                        logger.warning(f"Free transcription API returned {response.status_code}")

        except Exception as e:
            logger.error(f"Error transcribing voice (free): {e}")

    # ── LEVEL 4: OpenAI-compatible whisper endpoint fallback ──
    if api_key:
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                with open(file_path, "rb") as audio_file:
                    response = await client.post(
                        f"{config.POLLINATIONS_BASE_URL}/openai/audio/transcriptions",
                        files={"file": (os.path.basename(file_path), audio_file, "audio/ogg")},
                        data={"model": "whisper-1", "language": language},
                        headers={"Authorization": f"Bearer {api_key}"},
                    )

                    if response.status_code == 200:
                        result = response.json()
                        text = result.get("text", "")
                        if text:
                            return text

        except Exception as e:
            logger.error(f"Fallback transcription error: {e}")

    # Clean up temp file
    try:
        os.unlink(file_path)
    except OSError:
        pass

    return None


async def process_voice_message(bot, file_id: str, language: str = "ru") -> str:
    """
    Full voice message processing pipeline: download + transcribe.
    Returns transcribed text or error message.
    """
    file_path = await download_voice_file(bot, file_id)
    if not file_path:
        return "Не удалось скачать голосовое сообщение. Пожалуйста, напишите текстом."

    text = await transcribe_voice(file_path, language)

    # Clean up
    try:
        if os.path.exists(file_path):
            os.unlink(file_path)
    except OSError:
        pass

    if text:
        return text
    else:
        return "Не удалось распознать голосовое сообщение. Попробуйте ещё раз или напишите текстом."
