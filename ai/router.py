"""
AI Router — Routes requests to the best available AI provider.
Pollinations primary with automatic fallback.
Supports chat, vision, VIN decoding, channel content, and more.
"""

import hashlib
import logging
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


class AIRouter:
    """Routes AI requests to the best available provider with fallback."""

    def __init__(self):
        self.providers: List[BaseAIProvider] = []
        self._primary: Optional[PollinationsProvider] = None

    async def initialize(self) -> None:
        """Initialize all providers and set primary."""
        pollinations = PollinationsProvider()
        self.providers = [pollinations]
        self._primary = pollinations
        logger.info(
            f"AI Router initialized with Pollinations as primary "
            f"({len(POLLINATIONS_MODELS)} models: "
            f"{len(CHAT_MODELS)} chat, {len(VISION_MODELS)} vision, "
            f"{len(CONTENT_MODELS)} content, {len(SEARCH_MODELS)} search)"
        )

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

        Args:
            user_id: Telegram user ID
            message: User's message text
            system_prompt: Override system prompt
            model: Override model
            temperature: Override temperature
            max_tokens: Override max tokens
            use_cache: Whether to check AI cache
            save_history: Whether to save to chat history
            extra_context: Additional context to append to system prompt

        Returns:
            AIResponse with the AI's reply
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

        # Try primary provider
        response = await self._primary.chat(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )

        # If primary failed, try fallback models
        if response.error:
            fallback_models = ["mistral-4", "deepseek", "nova-fast", "grok", "minimax",
                              "llama-scout", "gemma", "kimi", "glm", "mistral-small"]
            for fallback_model in fallback_models:
                if fallback_model == model:
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

        # Save to history
        if save_history and not response.error:
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

        return await self.chat(
            user_id=user_id,
            message=f"Расшифруй VIN: {vin_clean}",
            system_prompt=persona.system_prompt + persona.vin_prompt_suffix,
            model="openai-reasoning",  # Use reasoning model for VIN decoding
            temperature=0.3,  # More precise for VIN
            extra_context=vin_context,
        )

    def _parse_vin_basic(self, vin: str) -> str:
        """Parse basic VIN info (WMI - World Manufacturer Identifier)."""
        if len(vin) < 3:
            return ""

        wmi = vin[:3]
        wmi_map = {
            # Japanese
            "JHM": "Honda (Япония)", "JHN": "Honda (США)", "JHG": "Honda",
            "JT1": "Toyota", "JT2": "Toyota", "JT3": "Toyota", "JT4": "Toyota",
            "JT5": "Toyota", "JT6": "Toyota", "JT7": "Toyota", "JT8": "Toyota",
            "JN1": "Nissan (Япония)", "JN8": "Nissan",
            "JM1": "Mazda", "JM2": "Mazda", "JM3": "Mazda",
            "JF1": "Subaru", "JF2": "Subaru",
            "JS2": "Suzuki", "JS3": "Suzuki", "JS4": "Suzuki",
            "JA3": "Mitsubishi", "JA4": "Mitsubishi",
            # German
            "WBA": "BMW", "WBS": "BMW M", "WBX": "BMW SUV",
            "WVW": "Volkswagen", "WV1": "Volkswagen Commercial",
            "WAU": "Audi", "WAU": "Audi",
            "WDD": "Mercedes-Benz", "WDB": "Mercedes-Benz",
            "WP0": "Porsche",
            # American
            "1G1": "Chevrolet", "1G2": "Pontiac", "1G3": "Oldsmobile",
            "1G4": "Buick", "1G6": "Cadillac",
            "1FA": "Ford", "1FT": "Ford Truck", "1F1": "Ford",
            "2G1": "Chevrolet (Канада)", "2G2": "Pontiac (Канада)",
            "3FA": "Ford (Мексика)", "3FE": "Ford (Мексика)",
            # Korean
            "KMH": "Hyundai", "KNA": "Kia", "KNB": "Kia", "KNC": "Kia",
            "5NP": "Hyundai (США)", "5XM": "Hyundai",
            "KLA": "Hyundai", "KL1": "Chevrolet (Корея)",
            # Chinese
            "LBE": "BAIC", "LSG": "GM (Китай)", "LJ1": "FAW",
            "LVSH": "Great Wall", "LZG": "Geely",
            # Russian
            "XTA": "АвтоВАЗ (LADA)", "XTC": "АвтоВАЗ",
            "X7L": "Renault (Россия)", "X7M": "Hyundai (Россия)",
            "Z8T": "УАЗ",
            # Swedish
            "YV1": "Volvo", "YV4": "Volvo SUV",
            # French
            "VF1": "Renault", "VF3": "Peugeot", "VF7": "Citroen",
            # British
            "SAL": "Land Rover", "SAA": "Jaguar", "SCA": "Rolls-Royce",
            "SAJ": "Jaguar",
            # Italian
            "ZAR": "Alfa Romeo", "ZAM": "Maserati", "ZFF": "Ferrari",
            "ZFA": "Fiat",
            # Czech
            "TM9": "Škoda", "TMB": "Škoda",
        }

        manufacturer = wmi_map.get(wmi, "")
        if manufacturer:
            return f"Производитель (WMI {wmi}): {manufacturer}"

        return ""

    async def generate_channel_post(
        self,
        topic: str,
        source_text: str = "",
        extra_instructions: str = "",
        model: str = "",
        has_media: bool = False,
    ) -> AIResponse:
        """
        Generate a post for the @sochiautoparts channel.
        Uses a channel-specific prompt with proper footer format.
        Respects Telegram character limits: 1024 with media, 4096 without.
        """
        system_prompt = persona.system_prompt + persona.channel_prompt_suffix

        # Add time context
        time_ctx = _get_time_context()
        system_prompt += f"\n\n{time_ctx}"

        # Add character limit instruction
        char_limit = config.TELEGRAM_CAPTION_LIMIT if has_media else config.TELEGRAM_TEXT_LIMIT
        system_prompt += (
            f"\n\nВАЖНО: Лимит символов для поста — {char_limit}. "
            f"Пиши в пределах этого лимита, не превышай! "
            f"{'Это пост с фото/видео — текст будет подписью к медиа.' if has_media else 'Это текстовый пост без медиа.'}"
        )

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

        # Ensure footer is present with proper format (matching @sochiautoparts)
        if response.text and not response.error:
            text = response.text

            # Clean markdown-style links - convert [text](url) to plain text
            import re
            text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'\1', text)  # Remove markdown links
            text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)  # Remove bold
            text = re.sub(r'\*(.+?)\*', r'\1', text)  # Remove italic

            if "#sochiautoparts" not in text:
                text = text.rstrip() + "\n\nАвтор @asiaexp_bot\n@sochiautoparts\n#sochiautoparts"
            elif "@asiaexp_bot" not in text:
                # Insert Author mention before @sochiautoparts
                text = text.replace("@sochiautoparts", "Автор @asiaexp_bot\n@sochiautoparts")

            # Enforce character limit
            if has_media and len(text) > config.TELEGRAM_CAPTION_LIMIT:
                text = text[:config.TELEGRAM_CAPTION_LIMIT - 3] + "..."
            elif not has_media and len(text) > config.TELEGRAM_TEXT_LIMIT:
                text = text[:config.TELEGRAM_TEXT_LIMIT - 3] + "..."

            response.text = text

        return response

    async def diagnose_car(
        self,
        user_id: int,
        symptoms: str,
        car_info: str = "",
        model: str = "",
    ) -> AIResponse:
        """
        Generate a car diagnosis response.
        Uses reasoning model for complex diagnostics.
        """
        from bot.asya import build_diagnostic_context

        extra_context = build_diagnostic_context(symptoms)
        if car_info:
            extra_context = f"Информация об авто: {car_info}\n{extra_context}"

        return await self.chat(
            user_id=user_id,
            message=symptoms,
            system_prompt=persona.system_prompt + persona.diagnostic_prompt_suffix,
            model=model or "openai-reasoning",  # Use reasoning for diagnostics
            temperature=0.5,  # More precise for diagnostics
            extra_context=extra_context,
        )

    async def find_spare_part(
        self,
        user_id: int,
        article: str,
        part_info: str = "",
        model: str = "",
    ) -> AIResponse:
        """
        Generate a spare part search response.
        """
        extra_context = ""
        if part_info:
            extra_context = f"Информация о запчасти из каталогов:\n{part_info}"

        return await self.chat(
            user_id=user_id,
            message=f"Найди запчасть по артикулу: {article}",
            system_prompt=persona.system_prompt + persona.spare_part_prompt_suffix,
            model=model,
            temperature=0.4,  # Precise for part search
            extra_context=extra_context,
        )

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
