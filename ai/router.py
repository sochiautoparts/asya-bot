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
            fallback_models = ["openai-mini", "mistral-4", "deepseek", "nova-fast", "grok", "minimax",
                              "llama-scout", "gemma", "kimi", "glm", "mistral-small", "step-flash"]
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
        """Parse VIN info: WMI manufacturer, model year, assembly plant, check digit.
        
        VIN structure (17 chars):
        - Pos 1-3: WMI (World Manufacturer Identifier)
        - Pos 4-8: Vehicle Descriptor (model, body, engine)
        - Pos 9: Check digit (0-9 or X)
        - Pos 10: Model Year
        - Pos 11: Assembly Plant
        - Pos 12-17: Production Serial Number
        """
        if len(vin) < 3:
            return ""

        parts = []

        # ── WMI (Positions 1-3) ──
        wmi = vin[:3]
        wmi_map = {
            # Japanese
            "JHM": "Honda (Япония)", "JHN": "Honda (США)", "JHG": "Honda (Япония)",
            "JT1": "Toyota (Япония)", "JT2": "Toyota (Япония)", "JT3": "Toyota",
            "JT4": "Toyota", "JT5": "Toyota", "JT6": "Toyota", "JT7": "Toyota",
            "JT8": "Toyota", "JTD": "Toyota (США)", "JTE": "Toyota (США)",
            "JN1": "Nissan (Япония)", "JN8": "Nissan (США)", "JNK": "Infiniti (Япония)",
            "JM1": "Mazda (Япония)", "JM2": "Mazda (США)", "JM3": "Mazda (США)",
            "JF1": "Subaru (Япония)", "JF2": "Subaru (США)",
            "JS2": "Suzuki (Япония)", "JS3": "Suzuki", "JS4": "Suzuki",
            "JA3": "Mitsubishi (Япония)", "JA4": "Mitsubishi (США)",
            "JJA": "Isuzu (Япония)", "JAL": "Alfa Romeo (Япония)",
            # German
            "WBA": "BMW (Германия)", "WBS": "BMW M (Германия)", "WBX": "BMW SUV (Германия)",
            "WBY": "BMW i (Германия)", "WBA": "BMW (Германия)",
            "WVW": "Volkswagen (Германия)", "WV1": "Volkswagen Commercial",
            "WV2": "Volkswagen Bus/Van",
            "WAU": "Audi (Германия)", "WAU": "Audi (Германия)",
            "WDD": "Mercedes-Benz (Германия)", "WDB": "Mercedes-Benz (Германия)",
            "WDC": "Mercedes-Benz (США)", "WDF": "Mercedes-Benz Van",
            "WP0": "Porsche (Германия)", "WP1": "Porsche SUV (Германия)",
            "W0L": "Opel (Германия)", "W0V": "Opel (Германия)",
            # American
            "1G1": "Chevrolet (США)", "1G2": "Pontiac (США)", "1G3": "Oldsmobile",
            "1G4": "Buick (США)", "1G6": "Cadillac (США)", "1GC": "Chevrolet Truck",
            "1FA": "Ford (США)", "1FT": "Ford Truck (США)", "1F1": "Ford (США)",
            "1FM": "Ford SUV (США)", "1FD": "Ford Commercial",
            "2G1": "Chevrolet (Канада)", "2G2": "Pontiac (Канада)",
            "2FA": "Ford (Канада)", "2FM": "Ford (Канада)",
            "3FA": "Ford (Мексика)", "3FE": "Ford (Мексика)",
            "3VW": "Volkswagen (Мексика)", "3MB": "Mitsubishi (Мексика)",
            "1N4": "Nissan (США)", "1N6": "Nissan Truck (США)",
            "1HG": "Honda (США)", "1HY": "Acura (США)",
            "1J4": "Jeep (США)", "1C4": "Chrysler (США)", "1C6": "RAM (США)",
            "5YJ": "Tesla (США)", "7SAY": "Tesla (США)",
            # Korean
            "KMH": "Hyundai (Корея)", "KNA": "Kia (Корея)", "KNB": "Kia (Корея)",
            "KNC": "Kia (Корея)", "KND": "Kia (США)",
            "5NP": "Hyundai (США)", "5XM": "Hyundai (США)",
            "5XY": "Kia (США)", "5XK": "Kia (США)",
            "KLA": "Hyundai (Корея)", "KL1": "Chevrolet (Корея)",
            "KNM": "Renault Samsung (Корея)", "KPH": "SsangYong (Корея)",
            # Chinese
            "LBE": "BAIC (Китай)", "LSG": "GM (Китай)", "LJ1": "FAW (Китай)",
            "LVSH": "Great Wall (Китай)", "LZG": "Geely (Китай)",
            "LFV": "Volkswagen (Китай)", "LSV": "Volkswagen (Китай)",
            "LGB": "Geely (Китай)", "LFP": "Chery (Китай)",
            "LJX": "Haval (Китай)", "LZW": "SAIC (Китай)",
            "LVV": "Chery (Китай)", "LDC": "Dongfeng Peugeot (Китай)",
            "LNB": "Brilliance (Китай)", "LJU": "BYD (Китай)",
            # Russian
            "XTA": "АвтоВАЗ LADA (Россия)", "XTC": "АвтоВАЗ (Россия)",
            "XTB": "АвтоВАЗ (Россия)", "XTD": "АвтоВАЗ (Россия)",
            "X7L": "Renault (Россия)", "X7M": "Hyundai (Россия)",
            "Z8T": "УАЗ (Россия)", "X7M": "Hyundai (Россия)",
            "XUF": "Chevrolet (Россия)", "XWB": "Kia (Россия)",
            "XWE": "Hyundai (Россия)", "XWU": "Renault (Россия)",
            # Swedish
            "YV1": "Volvo (Швеция)", "YV4": "Volvo SUV (Швеция)",
            "YV2": "Volvo Truck (Швеция)", "YV3": "Volvo Bus (Швеция)",
            # French
            "VF1": "Renault (Франция)", "VF3": "Peugeot (Франция)",
            "VF7": "Citroen (Франция)", "VF6": "Renault Truck (Франция)",
            "VF8": "Renault (Франция)",
            "VR1": "Renault (Франция)", "VR7": "Peugeot (Франция)",
            # British
            "SAL": "Land Rover (Великобритания)", "SAA": "Jaguar (Великобритания)",
            "SCA": "Rolls-Royce (Великобритания)", "SAJ": "Jaguar (Великобритания)",
            "SDB": "Aston Martin (Великобритания)", "SCC": "McLaren (Великобритания)",
            "SAX": "MG (Великобритания)",
            # Italian
            "ZAR": "Alfa Romeo (Италия)", "ZAM": "Maserati (Италия)",
            "ZFF": "Ferrari (Италия)", "ZFA": "Fiat (Италия)",
            "ZLA": "Lancia (Италия)", "ZAP": "Piaggio (Италия)",
            "ZCG": "Lamborghini (Италия)",
            # Czech
            "TM9": "Škoda (Чехия)", "TMB": "Škoda (Чехия)",
            "TMK": "Škoda (Чехия)", "TMP": "Škoda (Чехия)",
            # Spanish
            "VSS": "SEAT (Испания)", "VSE": "SEAT (Испания)",
        }

        manufacturer = wmi_map.get(wmi, "")
        if manufacturer:
            parts.append(f"Производитель (WMI {wmi}): {manufacturer}")
        else:
            # Try to determine region from first character
            first_char = vin[0]
            region_map = {
                "1": "Северная Америка (США)", "2": "Северная Америка (Канада)",
                "3": "Северная Америка (Мексика)", "4": "США",
                "5": "США", "6": "Австралия", "7": "Новая Зеландия",
                "8": "Южная Америка", "9": "Южная Америка",
                "J": "Япония", "K": "Корея", "L": "Китай",
                "M": "Индия", "N": "Индонезия/Турция",
                "P": "Европа/Азия", "R": "Тайвань/Вьетнам",
                "S": "Великобритания", "T": "Чехия/Венгрия",
                "U": "Дания/Финляндия", "V": "Франция/Испания",
                "W": "Германия", "X": "Россия/Нидерланды",
                "Y": "Швеция/Норвегия", "Z": "Италия/Бельгия",
            }
            region = region_map.get(first_char, "Неизвестный регион")
            parts.append(f"WMI {wmi} — регион: {region}")

        # ── Model Year (Position 10) ──
        if len(vin) >= 10:
            year_code = vin[9]
            year_map = {
                "A": 2010, "B": 2011, "C": 2012, "D": 2013, "E": 2014,
                "F": 2015, "G": 2016, "H": 2017, "J": 2018, "K": 2019,
                "L": 2020, "M": 2021, "N": 2022, "P": 2023, "R": 2024,
                "S": 2025, "T": 2026, "V": 2027, "W": 2028, "X": 2029,
                "Y": 2030, "1": 2001, "2": 2002, "3": 2003, "4": 2004,
                "5": 2005, "6": 2006, "7": 2007, "8": 2008, "9": 2009,
                "0": 2000,
            }
            model_year = year_map.get(year_code)
            if model_year:
                # Could be model_year or model_year+30 (e.g. 1980 = 2010)
                if model_year < 1990:
                    actual_year = model_year + 30  # 1980s cycle
                else:
                    actual_year = model_year
                parts.append(f"Модельный год: {actual_year} (код: {year_code})")

        # ── Check Digit Validation (Position 9) ──
        if len(vin) >= 9:
            check_char = vin[8]
            if check_char != "X" and not check_char.isdigit():
                parts.append(f"⚠️ Контрольный символ ({check_char}) выглядит некорректно")
            else:
                is_valid = self._validate_vin_check_digit(vin)
                if not is_valid and len(vin) == 17:
                    parts.append("⚠️ Контрольная сумма VIN не совпадает — возможна ошибка в коде")
                elif len(vin) == 17:
                    parts.append("✅ Контрольная сумма VIN корректна")

        # ── Assembly Plant (Position 11) ──
        if len(vin) >= 11:
            plant_code = vin[10]
            parts.append(f"Код завода сборки: {plant_code}")

        # ── Serial Number (Positions 12-17) ──
        if len(vin) >= 17:
            serial = vin[11:17]
            parts.append(f"Серийный номер: {serial}")

        return "\n".join(parts) if parts else ""

    @staticmethod
    def _validate_vin_check_digit(vin: str) -> bool:
        """Validate VIN check digit (position 9).
        
        Uses the standard VIN check digit algorithm:
        - Assign weights to each position
        - Multiply each digit value by its weight
        - Sum all products
        - Check digit = sum mod 11 (X for 10)
        """
        if len(vin) != 17:
            return False

        # Position weights
        weights = [8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2]

        # Character values (I, O, Q are not used in VIN)
        values = {
            "0": 0, "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7,
            "8": 8, "9": 9,
            "A": 1, "B": 2, "C": 3, "D": 4, "E": 5, "F": 6, "G": 7, "H": 8,
            "J": 1, "K": 2, "L": 3, "M": 4, "N": 5, "P": 7, "R": 9,
            "S": 2, "T": 3, "U": 4, "V": 5, "W": 6, "X": 7, "Y": 8, "Z": 9,
        }

        vin_upper = vin.upper()

        total = 0
        for i in range(17):
            char = vin_upper[i]
            if char not in values:
                return False
            total += values[char] * weights[i]

        expected_check = total % 11
        if expected_check == 10:
            expected_char = "X"
        else:
            expected_char = str(expected_check)

        return vin_upper[8] == expected_char

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
        Respects Telegram character limits: 1024 with media, 4096 without.
        Supports up to 10 media files per post (media group/album).
        """
        system_prompt = persona.system_prompt + persona.channel_prompt_suffix

        # Add time context
        time_ctx = _get_time_context()
        system_prompt += f"\n\n{time_ctx}"

        # Add character limit instruction — very explicit for AI
        char_limit = config.TELEGRAM_CAPTION_LIMIT if has_media else config.TELEGRAM_TEXT_LIMIT
        footer_chars = 55  # Approx chars for "Автор @asiaexp_bot\n@sochiautoparts\n#sochiautoparts"
        content_limit = char_limit - footer_chars

        if has_media:
            limit_instruction = (
                f"\n\nКРИТИЧЕСКИ ВАЖНО — ЛИМИТ СИМВОЛОВ:\n"
                f"Это пост С медиа (фото/видео). Текст будет подписью к медиа.\n"
                f"МАКСИМУМ 1024 символа ВЕСЬ пост, включая подпись.\n"
                f"Подпись 'Автор @asiaexp_bot / @sochiautoparts / #sochiautoparts' занимает ~55 символов.\n"
                f"Значит твой полезный текст — НЕ БОЛЕЕ {content_limit} символов.\n"
                f"Пиши КОМПАКТНО и ЁМКО. Не растягивай. Лучше короче, чем обрезать.\n"
                f"Подпись в конце ОБЯЗАТЕЛЬНА — никогда не обрезай её."
            )
            if media_count > 1:
                limit_instruction += (
                    f"\n\nК посту будет прикреплено {media_count} фото/видео (альбом/карусель)."
                    f"Текст пишется как подпись к первому медиа — один на весь пост."
                )
        else:
            limit_instruction = (
                f"\n\nЛИМИТ СИМВОЛОВ:\n"
                f"Это текстовый пост БЕЗ медиа. Максимум 4096 символов весь пост.\n"
                f"Подпись 'Автор @asiaexp_bot / @sochiautoparts / #sochiautoparts' занимает ~55 символов.\n"
                f"Значит твой полезный текст — НЕ БОЛЕЕ {content_limit} символов.\n"
                f"Подпись в конце ОБЯЗАТЕЛЬНА — никогда не обрезай её."
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

            # Enforce character limit — smart truncation preserving footer
            footer = "\n\nАвтор @asiaexp_bot\n@sochiautoparts\n#sochiautoparts"
            if has_media and len(text) > config.TELEGRAM_CAPTION_LIMIT:
                # Strip existing footer, truncate content, re-add footer
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
