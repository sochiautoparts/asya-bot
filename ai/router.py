"""
AI Router — Routes requests to the best available AI provider.
Pollinations primary with automatic fallback.
"""

import hashlib
import logging
from typing import Optional, List, Dict

from ai.providers.base import BaseAIProvider, AIResponse
from ai.providers.pollinations_provider import PollinationsProvider, POLLINATIONS_MODELS
from bot.config import config, persona
from bot.database import get_ai_cached, set_ai_cached, get_chat_history, add_chat_message

logger = logging.getLogger("asya.ai.router")


class AIRouter:
    """Routes AI requests to the best available provider with fallback."""

    def __init__(self):
        self.providers: List[BaseAIProvider] = []
        self._primary: Optional[BaseAIProvider] = None

    async def initialize(self) -> None:
        """Initialize all providers and set primary."""
        pollinations = PollinationsProvider()
        self.providers = [pollinations]
        self._primary = pollinations
        logger.info("AI Router initialized with Pollinations as primary")

    @property
    def primary(self) -> Optional[BaseAIProvider]:
        return self._primary

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

        # Build system prompt
        sys_prompt = system_prompt or persona.system_prompt
        if extra_context:
            sys_prompt += f"\n\n{extra_context}"

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

        # Get chat history
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

        # If primary failed, try fallback models in priority order
        if response.error:
            for fallback_model in ["mistral-4", "deepseek", "nova-fast", "grok", "minimax", "llama-scout", "gemma"]:
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

    async def generate_channel_post(
        self,
        topic: str,
        source_text: str = "",
        extra_instructions: str = "",
        model: str = "",
    ) -> AIResponse:
        """
        Generate a post for the @sochiautoparts channel.
        Uses a channel-specific prompt with proper footer format.
        """
        system_prompt = persona.system_prompt + persona.channel_prompt_suffix

        user_content = f"Тема для поста: {topic}"
        if source_text:
            user_content += f"\n\nИсходный текст/новость:\n{source_text}"
        if extra_instructions:
            user_content += f"\n\nДополнительные инструкции: {extra_instructions}"

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

        response = await self._primary.chat(
            messages=messages,
            model=model or "openai-large",
            temperature=0.8,
            max_tokens=1500,
        )

        # Ensure footer is present with proper format
        if response.text and not response.error:
            if "#sochiautoparts" not in response.text:
                response.text += "\n\n#sochiautoparts"
            if "asiaexp_bot" not in response.text:
                response.text = response.text.rstrip() + "\n\n[Ася - Автоэксперт](https://t.me/asiaexp_bot)\n@sochiautoparts"
            if "@sochiautoparts" not in response.text:
                response.text = response.text.rstrip() + "\n@sochiautoparts"

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
        """
        from bot.asya import build_diagnostic_context

        extra_context = build_diagnostic_context(symptoms)
        if car_info:
            extra_context = f"Информация об авто: {car_info}\n{extra_context}"

        return await self.chat(
            user_id=user_id,
            message=symptoms,
            system_prompt=persona.system_prompt + persona.diagnostic_prompt_suffix,
            model=model,
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


# ── Global instance ────────────────────────────────────────────────────────────

ai_router = AIRouter()
