"""
AI Router — Routes requests to the best available AI provider.
Pollinations primary + LlamaCpp local fallback.
Supports chat, vision, VIN decoding, channel content, and more.
"""

import hashlib
import logging
import random
from typing import Optional, List, Dict
from datetime import datetime
from zoneinfo import ZoneInfo

from ai.providers.base import BaseAIProvider, AIResponse
from ai.providers.pollinations_provider import (
    PollinationsProvider, POLLINATIONS_MODELS,
    CHAT_MODELS, REASONING_MODELS, VISION_MODELS,
    CONTENT_MODELS, SEARCH_MODELS, IMAGE_MODELS,
)
from bot.config import config, persona
from bot.database import get_ai_cached, set_ai_cached, get_chat_history, add_chat_message

# Conditional import — LlamaCpp local fallback
try:
    from ai.providers.llama_cpp_provider import LlamaCppProvider
    _LLAMA_CPP_AVAILABLE = True
except ImportError:
    LlamaCppProvider = None
    _LLAMA_CPP_AVAILABLE = False

logger = logging.getLogger("asya.ai.router")

# Moscow timezone
_MOSCOW_TZ = ZoneInfo("Europe/Moscow")


def _moscow_now() -> datetime:
    """Get current Moscow time."""
    return datetime.now(_MOSCOW_TZ)


def _get_time_context() -> str:
    """Get current Moscow time context for AI system prompt."""
    now = _moscow_now()
    hour = now.hour

    if 5 <= hour < 12:
        time_of_day = "утро"
        mood = "ты только проснулась, пьёшь кофе"
    elif 12 <= hour < 18:
        time_of_day = "день"
        mood = "ты бодрая, активная, в середине рабочего дня"
    elif 18 <= hour < 23:
        time_of_day = "вечер"
        mood = "ты устала за день, но рада поболтать"
    else:
        time_of_day = "ночь"
        mood = "ты не можешь уснуть, сова"

    weekday_names = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    weekday = weekday_names[now.weekday()]

    return (
        f"Сейчас {now.strftime('%d.%m.%Y')} {now.strftime('%H:%M')} по московскому времени. "
        f"День недели: {weekday}. Время суток: {time_of_day}. {mood}."
    )


# Static fallback responses for when ALL providers fail
FALLBACK_RESPONSES = [
    "Ммм... Ася задумалась. Повтори? 🤔",
    "Ой, Ася отвлеклась... Что ты сказал? 😅",
    "Блин, Ася задумалась о вечном... Ещё раз? 💅",
    "Ася не расслышала... Говори ещё! 😏",
    "Ой, мысли улетели! Повтори для Аси? 💭",
]


class AIRouter:
    """Routes AI requests to the best available provider with fallback.

    Route: Pollinations (primary) → LlamaCpp (local fallback) → static fallback.
    """

    def __init__(self):
        self.providers: List[BaseAIProvider] = []
        self._primary: Optional[PollinationsProvider] = None
        self._local: Optional[LlamaCppProvider] = None
        self._total_fallbacks: int = 0
        self._local_requests: int = 0

    async def initialize(self) -> None:
        """Initialize all providers: Pollinations PRIMARY + LlamaCpp FALLBACK."""
        # ── 1. Pollinations — PRIMARY ──
        pollinations = PollinationsProvider()
        self.providers = [pollinations]
        self._primary = pollinations
        logger.info(
            f"AI Router: Pollinations initialized as PRIMARY "
            f"({len(POLLINATIONS_MODELS)} models: "
            f"{len(CHAT_MODELS)} chat, {len(VISION_MODELS)} vision, "
            f"{len(CONTENT_MODELS)} content, {len(SEARCH_MODELS)} search)"
        )

        # ── 2. LlamaCpp — LOCAL FALLBACK (only if enabled AND available!) ──
        if config.ENABLE_LOCAL_MODEL and config.MODEL_PATH and _LLAMA_CPP_AVAILABLE and LlamaCppProvider is not None:
            try:
                self._local = LlamaCppProvider(
                    model_path=config.MODEL_PATH,
                    timeout=65.0,
                    model_config={
                        "n_ctx": config.MODEL_N_CTX,
                        "n_threads": config.MODEL_N_THREADS,
                        "n_gpu_layers": 0,
                        "verbose": False,
                        "use_mmap": True,
                        "use_mlock": False,
                    },
                    gen_config={
                        "max_tokens": min(config.MODEL_MAX_TOKENS, 256),
                        "temperature": 0.82,
                        "top_p": 0.92,
                        "top_k": 50,
                        "repeat_penalty": 1.12,
                    },
                )
                await self._local.init()
                logger.info("LlamaCppProvider initialized as LOCAL FALLBACK")
            except Exception as e:
                logger.warning(f"LlamaCppProvider init failed: {e}")
                self._local = None
        else:
            if not _LLAMA_CPP_AVAILABLE:
                logger.info("llama-cpp-python not installed — running cloud-only")
            elif config.ENABLE_LOCAL_MODEL:
                logger.info("ENABLE_LOCAL_MODEL=true but no MODEL_PATH — running cloud-only")
            else:
                logger.info("Local model DISABLED (ENABLE_LOCAL_MODEL not set) — running cloud-only")

        # Log status
        pollinations_status = "active" if self._primary and self._primary.is_available() else "unavailable"
        local_status = "not_installed" if not _LLAMA_CPP_AVAILABLE else ("disabled" if not config.ENABLE_LOCAL_MODEL else ("active" if self._local and self._local.is_available() else "unavailable"))
        model_name = self._local._model_name if self._local and self._local._loaded else "none"

        logger.info(
            f"AI Router initialized: "
            f"pollinations={pollinations_status} (PRIMARY, {len(CHAT_MODELS)} models, vision=yes), "
            f"local={local_status} (FALLBACK, model={model_name}, ENABLE_LOCAL_MODEL={config.ENABLE_LOCAL_MODEL})"
        )

    async def close(self) -> None:
        """Close all providers."""
        if self._local:
            try:
                await self._local.close()
            except Exception:
                pass

    @property
    def primary(self) -> Optional[BaseAIProvider]:
        return self._primary

    def _build_system_prompt(self, base_prompt: str = "", extra_context: str = "") -> str:
        """Build full system prompt with time context and extra context."""
        sys_prompt = base_prompt or persona.system_prompt

        # Always add Moscow time context
        time_ctx = _get_time_context()
        sys_prompt += f"\n\n{time_ctx}"

        if extra_context:
            sys_prompt += f"\n\n{extra_context}"

        return sys_prompt

    async def chat(
        self,
        user_id: int,
        message: str,
        system_prompt: str = "",
        model: str = "",
        temperature: float = None,
        max_tokens: int = None,
        use_cache: bool = True,
        save_history: bool = True,
        extra_context: str = "",
    ) -> AIResponse:
        """
        Send a chat message through the AI router.

        Route: Pollinations (primary) → LlamaCpp (local fallback) → static fallback.
        """
        temperature = temperature or config.CHAT_TEMPERATURE
        max_tokens = max_tokens or config.CHAT_MAX_TOKENS

        # Build system prompt with time context
        sys_prompt = self._build_system_prompt(system_prompt, extra_context)

        # Check cache first
        if use_cache:
            cache_key = self._make_cache_key(sys_prompt, message)
            cached = await get_ai_cached(cache_key)
            if cached:
                return AIResponse(
                    text=cached,
                    model="cached",
                    provider="cache",
                    cached=True,
                )

        # Get chat history for context
        history = await get_chat_history(user_id)

        # Format messages
        messages = self._primary.format_messages(sys_prompt, history, message)

        # ── 1. Try Pollinations PRIMARY ──
        response = await self._primary.chat(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )

        # If primary failed, try fallback models with SMART switching
        if response.error:
            is_auth_error = any(code in (response.error_message or "")
                               for code in ["401", "402", "unavailable", "cooldown"])

            if is_auth_error:
                # Auth/balance errors — try free-tier fallbacks
                from ai.providers.pollinations_provider import PRIORITY_FREE_MODELS
                fallback_models = [m for m in PRIORITY_FREE_MODELS
                                   if m != model and not self._primary._is_model_in_cooldown(m)][:2]
                if fallback_models:
                    logger.info(f"Auth error, trying {len(fallback_models)} priority free fallbacks")
            else:
                # Other errors (timeout, server error) — try broader fallback but limit to 3 attempts
                fallback_models = [
                    "mistral-small", "deepseek-v4", "llama-3.3", "nova-fast",
                    "gemma", "qwen3-coder", "step-3.5-flash",
                ][:3]
                logger.info(f"Non-auth error, trying fallbacks: {fallback_models}")

            for fallback_model in fallback_models:
                if fallback_model == model:
                    continue
                if self._primary._is_model_in_cooldown(fallback_model):
                    continue
                logger.info(f"Trying fallback model: {fallback_model}")
                response = await self._primary.chat(
                    messages=messages,
                    model=fallback_model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                if not response.error:
                    break

        # ── 2. If Pollinations STILL failed, try LOCAL FALLBACK ──
        if response.error and self._local and self._local.is_available():
            logger.warning("Pollinations failed, falling back to LOCAL model")
            try:
                local_response = await self._local.chat(
                    messages=messages,
                    model="",
                    temperature=temperature or 0.82,
                    max_tokens=min(max_tokens or 256, 256),
                )
                if not local_response.error and local_response.text:
                    self._local_requests += 1
                    # Clean the response (strip think tags etc.)
                    text = self._clean_ai_response(local_response.text)
                    if text:
                        response = AIResponse(
                            text=text,
                            model=local_response.model,
                            provider=local_response.provider,
                            tokens_used=local_response.tokens_used,
                        )
                        logger.info(f"LOCAL FALLBACK succeeded: model={local_response.model}")
            except Exception as e:
                logger.warning(f"Local model chat error: {e}")

        # ── 3. If ALL AI providers failed, use static fallback ──
        if response.error:
            self._total_fallbacks += 1
            logger.error("All AI providers unavailable! Using static fallback.")
            response = AIResponse(
                text=random.choice(FALLBACK_RESPONSES),
                model="fallback",
                provider="static",
                tokens_used=0,
            )

        # Save to history
        if save_history and not response.error and response.text:
            await add_chat_message(user_id, "user", message)
            await add_chat_message(user_id, "assistant", response.text)

            # Cache the response
            if use_cache and response.text:
                cache_key = self._make_cache_key(sys_prompt, message)
                await set_ai_cached(cache_key, message, response.text, response.model)

        return response

    async def analyze_image(
        self,
        user_id: int,
        image_url: str = "",
        image_base64: str = "",
        prompt: str = "",
        extra_context: str = "",
    ) -> AIResponse:
        """
        Analyze an image using vision-capable models.
        Local model can't see images — Pollinations only.
        """
        # Build the vision prompt
        if not prompt:
            prompt = "Рассмотри это изображение внимательно и подробно опиши что на нём."

        sys_prompt = self._build_system_prompt(
            persona.system_prompt + persona.vision_prompt_suffix,
            extra_context,
        )

        # Get chat history for context
        history = await get_chat_history(user_id)

        # Build context from history
        context_summary = ""
        if history:
            recent = history[-6:]  # Last 6 messages for context
            for msg in recent:
                role = "Пользователь" if msg.get("role") == "user" else "Ася"
                content = msg.get("content", "")[:100]
                if content:
                    context_summary += f"{role}: {content}\n"

        if context_summary:
            sys_prompt += f"\n\nКонтекст недавней беседы:\n{context_summary}"

        response = await self._primary.analyze_image(
            image_url=image_url,
            image_base64=image_base64,
            prompt=prompt,
            model="openai",  # Primary vision model
            system_prompt=sys_prompt,
            max_tokens=800,
            temperature=0.7,
        )

        # If vision failed, local model can't help (no vision support)
        if response.error:
            response = AIResponse(
                text="Ой, не получилось разглядеть фото 😅 Попробуй ещё раз!",
                model="fallback",
                provider="static",
                error=True,
                error_message=response.error_message,
            )

        # Save to history
        if not response.error and response.text:
            prompt_text = prompt if len(prompt) < 100 else prompt[:97] + "..."
            await add_chat_message(user_id, "user", f"[Фото] {prompt_text}")
            await add_chat_message(user_id, "assistant", response.text)

        return response

    async def decode_vin(
        self,
        user_id: int,
        vin_code: str,
        extra_context: str = "",
    ) -> AIResponse:
        """
        Decode a VIN code or body number for vehicle information.
        Falls back to local model if Pollinations is unavailable.
        """
        # Clean VIN code
        vin_clean = vin_code.strip().upper()

        # Build VIN-specific context
        vin_context = f"VIN-код или номер кузова для расшифровки: {vin_clean}\n"

        # Try to extract basic info from VIN pattern
        vin_info = self._parse_vin_basic(vin_clean)
        if vin_info:
            vin_context += f"Предварительные данные: {vin_info}\n"

        if extra_context:
            vin_context += f"\n{extra_context}"

        # Use a strong model for VIN, but fall back to free tier if balance depleted
        vin_model = "gpt-5.5"
        if self._primary._balance_depleted_at is not None:
            from ai.providers.pollinations_provider import PRIORITY_FREE_MODELS
            vin_model = PRIORITY_FREE_MODELS[0]  # Use free model when balance is low
            logger.info(f"Balance depleted, using free model {vin_model} for VIN decode")

        return await self.chat(
            user_id=user_id,
            message=f"Расшифруй VIN: {vin_clean}",
            system_prompt=persona.system_prompt + persona.vin_prompt_suffix,
            model=vin_model,
            temperature=0.3,  # More precise for VIN
            extra_context=vin_context,
        )

    def _parse_vin_basic(self, vin: str) -> str:
        """Parse VIN info: WMI manufacturer, model year, assembly plant, check digit."""
        if len(vin) < 3:
            return ""

        parts = []

        # WMI
        wmi = vin[:3]
        wmi_map = {
            "JHM": "Honda (Япония)", "JHN": "Honda (США)", "JHG": "Honda (Япония)",
            "JT1": "Toyota (Япония)", "JT2": "Toyota (Япония)", "JTD": "Toyota (США)",
            "JN1": "Nissan (Япония)", "JN8": "Nissan (США)", "JNK": "Infiniti (Япония)",
            "JM1": "Mazda (Япония)", "JF1": "Subaru (Япония)",
            "WBA": "BMW (Германия)", "WBS": "BMW M (Германия)",
            "WVW": "Volkswagen (Германия)", "WAU": "Audi (Германия)",
            "WDD": "Mercedes-Benz (Германия)", "WDB": "Mercedes-Benz (Германия)",
            "WP0": "Porsche (Германия)",
            "1G1": "Chevrolet (США)", "1FA": "Ford (США)", "1FT": "Ford Truck (США)",
            "1HG": "Honda (США)", "1N4": "Nissan (США)",
            "1J4": "Jeep (США)", "1C4": "Chrysler (США)", "1C6": "RAM (США)",
            "5YJ": "Tesla (США)",
            "KMH": "Hyundai (Корея)", "KNA": "Kia (Корея)", "KND": "Kia (США)",
            "XTA": "АвтоВАЗ LADA (Россия)", "Z8T": "УАЗ (Россия)",
            "YV1": "Volvo (Швеция)",
            "VF1": "Renault (Франция)", "VF3": "Peugeot (Франция)", "VF7": "Citroen (Франция)",
            "SAL": "Land Rover (Великобритания)", "SAA": "Jaguar (Великобритания)",
            "ZAR": "Alfa Romeo (Италия)", "ZAM": "Maserati (Италия)",
            "ZFF": "Ferrari (Италия)", "ZFA": "Fiat (Италия)",
            "TM9": "Škoda (Чехия)", "TMB": "Škoda (Чехия)",
        }

        manufacturer = wmi_map.get(wmi, "")
        if manufacturer:
            parts.append(f"Производитель (WMI {wmi}): {manufacturer}")
        else:
            region_map = {
                "1": "США", "2": "Канада", "3": "Мексика",
                "J": "Япония", "K": "Корея", "L": "Китай",
                "S": "Великобритания", "V": "Франция/Испания",
                "W": "Германия", "X": "Россия/Нидерланды", "Y": "Швеция/Норвегия",
                "Z": "Италия/Бельгия",
            }
            region = region_map.get(vin[0], "Неизвестный регион")
            parts.append(f"WMI {wmi} — регион: {region}")

        # Model Year
        if len(vin) >= 10:
            year_code = vin[9]
            year_map = {
                "A": 2010, "B": 2011, "C": 2012, "D": 2013, "E": 2014,
                "F": 2015, "G": 2016, "H": 2017, "J": 2018, "K": 2019,
                "L": 2020, "M": 2021, "N": 2022, "P": 2023, "R": 2024,
                "S": 2025, "T": 2026, "V": 2027,
            }
            model_year = year_map.get(year_code)
            if model_year:
                parts.append(f"Модельный год: {model_year} (код: {year_code})")

        # Serial Number
        if len(vin) >= 17:
            serial = vin[11:17]
            parts.append(f"Серийный номер: {serial}")

        return "\n".join(parts) if parts else ""

    async def generate_channel_post(
        self,
        topic: str,
        source_text: str = "",
        extra_instructions: str = "",
        model: str = "",
        has_media: bool = False,
        media_count: int = 0,
    ) -> AIResponse:
        """
        Generate a post for the @sochiautoparts channel.
        Uses a channel-specific prompt with proper footer format.
        """
        system_prompt = persona.system_prompt + persona.channel_prompt_suffix

        # Add time context
        time_ctx = _get_time_context()
        system_prompt += f"\n\n{time_ctx}"

        # Add character limit instruction
        char_limit = config.TELEGRAM_CAPTION_LIMIT if has_media else config.TELEGRAM_TEXT_LIMIT
        footer_chars = 55
        content_limit = char_limit - footer_chars

        if has_media:
            limit_instruction = (
                f"\n\nКРИТИЧЕСКИ ВАЖНО — ЛИМИТ СИМВОЛОВ:\n"
                f"Это пост С медиа. Максимум 1024 символа ВЕСЬ пост.\n"
                f"Подпись 'Автор @asiaexp_bot / @sochiautoparts / #sochiautoparts' занимает ~55 символов.\n"
                f"Значит твой полезный текст — НЕ БОЛЕЕ {content_limit} символов.\n"
                f"Подпись в конце ОБЯЗАТЕЛЬНА — никогда не обрезай её."
            )
        else:
            limit_instruction = (
                f"\n\nЛИМИТ СИМВОЛОВ:\n"
                f"Это текстовый пост БЕЗ медиа. Максимум 4096 символов весь пост.\n"
                f"Подпись в конце ОБЯЗАТЕЛЬНА."
            )

        system_prompt += limit_instruction

        user_content = f"Тема для поста: {topic}"
        if source_text:
            user_content += f"\n\nИсходный текст/новость:\n{source_text}"
        if extra_instructions:
            user_content += f"\n\nДополнительные инструкции: {extra_instructions}"

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

        # Use content model for channel posts
        post_model = model or "openai-large"

        response = await self._primary.chat(
            messages=messages,
            model=post_model,
            temperature=0.8,
            max_tokens=1500,
        )

        # If cloud failed, try local fallback for channel post
        if response.error and self._local and self._local.is_available():
            logger.warning("Pollinations failed for channel post, trying local model")
            try:
                local_response = await self._local.chat(
                    messages=messages,
                    model="",
                    temperature=0.8,
                    max_tokens=256,
                )
                if not local_response.error and local_response.text:
                    response = local_response
            except Exception as e:
                logger.warning(f"Local model channel post error: {e}")

        # Ensure footer is present
        if response.text and not response.error:
            text = response.text

            # Clean markdown-style links
            import re
            text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'\1', text)
            text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
            text = re.sub(r'\*(.+?)\*', r'\1', text)

            if "#sochiautoparts" not in text:
                text = text.rstrip() + "\n\nАвтор @asiaexp_bot\n@sochiautoparts\n#sochiautoparts"
            elif "@asiaexp_bot" not in text:
                text = text.replace("@sochiautoparts", "Автор @asiaexp_bot\n@sochiautoparts")

            # Enforce character limit
            footer = "\n\nАвтор @asiaexp_bot\n@sochiautoparts\n#sochiautoparts"
            if has_media and len(text) > config.TELEGRAM_CAPTION_LIMIT:
                for foot_part in ["\n\nАвтор @asiaexp_bot", "\n@sochiautoparts", "\n#sochiautoparts"]:
                    text = text.replace(foot_part, "")
                text = text.rstrip()
                max_content = config.TELEGRAM_CAPTION_LIMIT - len(footer)
                if len(text) > max_content:
                    text = text[:max_content - 3] + "..."
                text += footer
            elif not has_media and len(text) > config.TELEGRAM_TEXT_LIMIT:
                for foot_part in ["\n\nАвтор @asiaexp_bot", "\n@sochiautoparts", "\n#sochiautoparts"]:
                    text = text.replace(foot_part, "")
                text = text.rstrip()
                max_content = config.TELEGRAM_TEXT_LIMIT - len(footer)
                if len(text) > max_content:
                    text = text[:max_content - 3] + "..."
                text += footer

            response.text = text

        return response

    async def diagnose_car(
        self,
        user_id: int,
        symptoms: str,
        car_info: str = "",
        model: str = "",
    ) -> AIResponse:
        """Generate a car diagnosis response. Falls back to local model if cloud unavailable."""
        from bot.asya import build_diagnostic_context

        extra_context = build_diagnostic_context(symptoms)
        if car_info:
            extra_context = f"Информация об авто: {car_info}\n{extra_context}"

        # Use reasoning model for diagnostics, but fall back to free tier if balance depleted
        diag_model = model or "gpt-5.5"
        if self._primary._balance_depleted_at is not None:
            from ai.providers.pollinations_provider import PRIORITY_FREE_MODELS
            diag_model = PRIORITY_FREE_MODELS[0]
            logger.info(f"Balance depleted, using free model {diag_model} for diagnostics")

        return await self.chat(
            user_id=user_id,
            message=symptoms,
            system_prompt=persona.system_prompt + persona.diagnostic_prompt_suffix,
            model=diag_model,
            temperature=0.5,
            extra_context=extra_context,
        )

    async def find_spare_part(
        self,
        user_id: int,
        article: str,
        part_info: str = "",
        model: str = "",
    ) -> AIResponse:
        """Generate a spare part search response. Falls back to local model if cloud unavailable."""
        extra_context = ""
        if part_info:
            extra_context = f"Информация о запчасти из каталогов:\n{part_info}"

        return await self.chat(
            user_id=user_id,
            message=f"Найди запчасть по артикулу: {article}",
            system_prompt=persona.system_prompt + persona.spare_part_prompt_suffix,
            model=model,
            temperature=0.4,
            extra_context=extra_context,
        )

    @staticmethod
    def _clean_ai_response(text: str) -> str:
        """Clean AI response artifacts (think tags, markdown, etc.)."""
        if not text:
            return ""

        # Strip think tags (Qwen3, reasoning models)
        import re
        text = re.sub(r'<think\b[^>]*>.*?</think\s*>', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<thinking\b[^>]*>.*?</thinking\s*>', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'</?think[^>]*>', '', text, flags=re.IGNORECASE)
        text = re.sub(r'</?thinking[^>]*>', '', text, flags=re.IGNORECASE)
        text = re.sub(r'<think\b[^>]*$', '', text, flags=re.IGNORECASE)

        # Strip /no_think prefix
        text = re.sub(r'^/no_think\s*', '', text)

        # Strip prefixes
        for prefix in ["Ася:", "Asya:", "АСЯ:", "Assistant:", "Ответ Аси:"]:
            if text.startswith(prefix):
                text = text[len(prefix):].strip()

        # Strip quotes
        if text.startswith('"') and text.endswith('"'):
            text = text[1:-1]
        if text.startswith("'") and text.endswith("'"):
            text = text[1:-1]

        text = text.strip("*").strip()

        # Strip markdown
        text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
        text = re.sub(r'\*([^*]+)\*', r'\1', text)
        text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)

        # Clean up whitespace
        text = re.sub(r'\n{3,}', '\n\n', text)
        text = text.strip()

        return text

    def _make_cache_key(self, system_prompt: str, message: str) -> str:
        """Create a cache key from system prompt and message."""
        content = f"{system_prompt[:200]}||{message[:500]}"
        return hashlib.sha256(content.encode()).hexdigest()

    def get_available_models(self) -> List[str]:
        """Get list of available models."""
        return POLLINATIONS_MODELS

    def get_model_categories(self) -> Dict[str, List[str]]:
        """Get models grouped by category."""
        return {
            "chat": CHAT_MODELS,
            "reasoning": REASONING_MODELS,
            "vision": VISION_MODELS,
            "content": CONTENT_MODELS,
            "search": SEARCH_MODELS,
            "image": IMAGE_MODELS,
        }


# ── Global instance ────────────────────────────────────────────────────────────

ai_router = AIRouter()
