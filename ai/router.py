"""
AI Router v8.0 — LOCAL-FIRST MULTI-PROVIDER FAILOVER with MODEL TIERING.

FAILOVER CHAIN (4 levels before static fallback):
  Level 0: Local Model (Qwen3-4B GGUF, CPU) — CHAT & COMMENT routes only
  Level 1: Pollinations (with API key) → KEY1 → KEY2
  Level 2: Pollinations FREE API (text.pollinations.ai, no auth)
  Level 3: Cloudflare Workers AI (@cf/mistralai/mistral-small-3.1-24b-instruct)
  Last resort: Static fallback responses

Route strategy (v8.0 — LOCAL-FIRST for simple tasks):
  CHAT route_type (user chats) → Local → Pollinations key → Pollinations free → Cloudflare → Static
  FUNCTION route_type (posts, VIN, diagnostics, parts) → Pollinations key → Pollinations free → Cloudflare → Local(fallback) → Static
  COMMENT route_type (comments) → Local → Pollinations key → Pollinations free → Cloudflare → Static
  VISION tasks (photos) → Pollinations vision (key) → Pollinations vision (free) → Cloudflare vision → Static
  IMAGE generation → Pollinations (key) → Pollinations free → None
  AUDIO transcription → Pollinations (key) → Pollinations free → None
"""

import hashlib
import logging
import random
import re
import time
from typing import Optional, List, Dict
from datetime import datetime
from zoneinfo import ZoneInfo

from ai.providers.base import BaseAIProvider, AIResponse
from ai.providers.local_provider import LocalProvider
from ai.providers.pollinations_provider import (
    PollinationsProvider, POLLINATIONS_MODELS,
    CHAT_MODELS, REASONING_MODELS, VISION_MODELS,
    CONTENT_MODELS, SEARCH_MODELS, IMAGE_MODELS, FALLBACK_MODELS,
)
from ai.providers.cloudflare_provider import CloudflareProvider
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


# Static fallback responses for when ALL providers fail
FALLBACK_RESPONSES = [
    "Ммм... Ася задумалась. Повтори? 🤔",
    "Ой, Ася отвлеклась... Что ты сказал? 😅",
    "Блин, Ася задумалась о вечном... Ещё раз? 💅",
    "Ася не расслышала... Говори ещё! 😏",
    "Ой, мысли улетели! Повтори для Аси? 💭",
]

# ── Model tiers for different route types ──

# Fast/cheap models for comments — low cost, quick responses
COMMENT_MODELS = ["mistral", "openai", "nova-fast", "mistral-small", "nova-micro"]

# Best quality models for function routes — accuracy matters
FUNCTION_MODELS = ["openai-large", "gpt-5.5", "deepseek"]


class AIRouter:
    """Routes AI requests through multiple providers with 4-level failover.

    v8.0 LOCAL-FIRST strategy:
    - Level 0: Local Model (Qwen3-4B GGUF) — CHAT & COMMENT routes
    - Level 1: Pollinations with API key (best quality, 60+ models)
    - Level 2: Pollinations FREE API (no auth, rate-limited)
    - Level 3: Cloudflare Workers AI (Mistral Small 3.1, 20K req/day)
    - Last resort: Static fallback responses
    """

    def __init__(self):
        self.providers: List[BaseAIProvider] = []
        self._local: Optional[LocalProvider] = None
        self._primary: Optional[PollinationsProvider] = None
        self._cloudflare: Optional[CloudflareProvider] = None
        self._total_fallbacks: int = 0
        self._total_requests: int = 0
        # Track which fallback level we're on for monitoring
        self._level0_count: int = 0  # Local model
        self._level1_count: int = 0  # Pollinations with key
        self._level2_count: int = 0  # Pollinations free
        self._level3_count: int = 0  # Cloudflare
        self._static_count: int = 0  # Static fallback

    async def initialize(self) -> None:
        """Initialize all providers and pre-load local model if enabled."""
        # Initialize local model provider (Level 0)
        self._local = LocalProvider()

        pollinations = PollinationsProvider()
        self._primary = pollinations

        # Initialize Cloudflare provider
        self._cloudflare = CloudflareProvider()

        self.providers = [self._local, pollinations]
        if self._cloudflare._accounts:
            self.providers.append(self._cloudflare)

        # Pre-load local model if enabled — this triggers auto-download if needed
        local_loaded = False
        if config.ENABLE_LOCAL_MODEL:
            try:
                import asyncio
                loop = asyncio.get_event_loop()
                local_loaded = await loop.run_in_executor(None, self._local._load_model)
            except Exception as e:
                logger.warning(f"Local model pre-load failed: {e}")

        local_status = self._local.get_status()
        cf_status = "active" if self._cloudflare._accounts else "not_configured"
        logger.info(
            f"AI Router v10.0 LOCAL-FIRST initialized: "
            f"local={local_status}, "
            f"pollinations=active (key+free), cloudflare={cf_status}, "
            f"failover: Local → Pollinations(key) → Pollinations(free) → Cloudflare → Static, "
            f"{len(POLLINATIONS_MODELS)} models: "
            f"{len(CHAT_MODELS)} chat, {len(VISION_MODELS)} vision, "
            f"{len(CONTENT_MODELS)} content, {len(SEARCH_MODELS)} search)"
        )

    async def close(self) -> None:
        """Close all providers."""
        pass

    @property
    def primary(self) -> Optional[BaseAIProvider]:
        return self._primary

    @property
    def cloudflare(self) -> Optional[CloudflareProvider]:
        return self._cloudflare

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
        route_type: str = "chat",
    ) -> AIResponse:
        """
        Send a chat message through the AI router with CONCURRENT failover.

        v9.0 CONCURRENT ROUTING (fast response, no sequential delays):
        - "chat" (default): Try Local first (if available), then CONCURRENT Pollinations+Cloudflare
        - "function": Try Pollinations key first, then CONCURRENT free+Cloudflare+Local
        - "comment": Try Local first, then CONCURRENT Pollinations+Cloudflare

        Key improvement: Instead of waiting for each level to timeout sequentially
        (which could take 30+15+60 = 105 seconds worst case), we now launch
        multiple providers CONCURRENTLY and return the FIRST successful response.
        This means if Pollinations is slow but Cloudflare responds in 3s, the user
        gets a response in ~3s instead of ~30s.
        """
        import asyncio as _asyncio
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

        # ── Select model based on route_type ──
        if route_type == "comment":
            model = model or "mistral"
        elif route_type == "function":
            model = model or "openai-large"
        else:
            model = model or ""

        # ── LEVEL 0: Local Model (Qwen3-4B) — CHAT & COMMENT routes ──
        # Local model is primary for simple chat and comments (saves cloud balance)
        # Skip for function routes (need cloud quality) and when explicitly disabled
        use_local_first = route_type in ("chat", "comment") and config.ENABLE_LOCAL_MODEL

        if use_local_first:
            # Quick check if local model is actually available (no timeout waste)
            local_available = (
                self._local is not None
                and self._local._model_loaded
                and self._local._llm is not None
                and self._local._consecutive_errors < 5
            )
            if local_available:
                response = await self._try_local(
                    user_id, message, history, sys_prompt, temperature, max_tokens
                )

                if not response.error:
                    self._level0_count += 1
                    self._total_requests += 1
                    return await self._save_response(user_id, message, response, sys_prompt, use_cache, save_history)

                logger.debug(f"Level 0 (local) failed for route={route_type}: {response.error_message}")

        # ── CONCURRENT FAILOVER: Launch multiple providers at once ──
        # This is the key fix for the "Asya takes too long to respond" issue.
        # Instead of trying providers one-by-one (each with 15-60s timeout),
        # we launch them concurrently and return the FIRST successful response.

        async def _safe_try_pollinations():
            """Try Pollinations with key — return response or error."""
            try:
                return await self._try_pollinations(
                    user_id, message, history, sys_prompt, temperature, max_tokens, model
                )
            except Exception as e:
                logger.error(f"Pollinations key exception: {e}")
                return AIResponse(text="", model=model, provider="pollinations", error=str(e), error_message=str(e))

        async def _safe_try_pollinations_free():
            """Try Pollinations free — return response or error."""
            try:
                return await self._try_pollinations_free(
                    user_id, message, history, sys_prompt, temperature, max_tokens, model
                )
            except Exception as e:
                logger.error(f"Pollinations free exception: {e}")
                return AIResponse(text="", model=model, provider="pollinations-free", error=str(e), error_message=str(e))

        async def _safe_try_cloudflare():
            """Try Cloudflare — return response or error."""
            try:
                return await self._try_cloudflare(
                    user_id, message, history, sys_prompt, temperature, max_tokens
                )
            except Exception as e:
                logger.error(f"Cloudflare exception: {e}")
                return AIResponse(text="", model="cloudflare", provider="cloudflare", error=str(e), error_message=str(e))

        async def _safe_try_local_fallback():
            """Try local model as fallback for function routes."""
            try:
                return await self._try_local(
                    user_id, message, history, sys_prompt, temperature, max_tokens
                )
            except Exception as e:
                logger.error(f"Local fallback exception: {e}")
                return AIResponse(text="", model="local-qwen3-4b", provider="local", error=str(e), error_message=str(e))

        # Build concurrent task list based on route type
        # Priority ordering: we want the best quality response first.
        # But we launch them ALL at once and take the first success.
        concurrent_tasks = []

        # Always try Pollinations with key (best quality when available)
        if self._primary and self._primary._build_key_tier_list():
            concurrent_tasks.append(("pollinations_key", _safe_try_pollinations()))

        # Always try Cloudflare in parallel (fast, independent service)
        if self._cloudflare and self._cloudflare._accounts:
            concurrent_tasks.append(("cloudflare", _safe_try_cloudflare()))

        # For function routes, also try local model as concurrent fallback
        if route_type == "function" and config.ENABLE_LOCAL_MODEL and self._local and self._local._model_loaded:
            concurrent_tasks.append(("local_fallback", _safe_try_local_fallback()))

        if not concurrent_tasks:
            # No providers available — try free API as last hope
            concurrent_tasks.append(("pollinations_free", _safe_try_pollinations_free()))

        # Execute all tasks concurrently, return FIRST successful response
        # Use asyncio.wait with FIRST_COMPLETED to get the fastest response
        task_names = [name for name, _ in concurrent_tasks]
        task_coros = [coro for _, coro in concurrent_tasks]
        tasks = [_asyncio.create_task(coro) for coro in task_coros]

        try:
            # Wait for the first task to complete
            done, pending = await _asyncio.wait(tasks, return_when=_asyncio.FIRST_COMPLETED)

            # Check if any completed task succeeded
            for task in done:
                response = task.result()
                if not response.error and response.text:
                    # Cancel remaining tasks (we have a winner!)
                    for p in pending:
                        p.cancel()
                    # Also cancel other done tasks we don't need
                    for t in tasks:
                        if t is not task and not t.done():
                            t.cancel()

                    # Track which level succeeded
                    task_idx = tasks.index(task)
                    level_name = task_names[task_idx]
                    if level_name == "pollinations_key":
                        self._level1_count += 1
                    elif level_name == "pollinations_free":
                        self._level2_count += 1
                    elif level_name == "cloudflare":
                        self._level3_count += 1
                    elif level_name == "local_fallback":
                        self._level0_count += 1

                    self._total_requests += 1
                    return await self._save_response(user_id, message, response, sys_prompt, use_cache, save_history)

            # First completed task(s) failed — wait for others
            # If there are still pending tasks, wait for them
            if pending:
                done2, pending2 = await _asyncio.wait(pending, return_when=_asyncio.FIRST_COMPLETED)
                done = done.union(done2)
                pending = pending2

                for task in done2:
                    response = task.result()
                    if not response.error and response.text:
                        for p in pending2:
                            p.cancel()
                        for t in tasks:
                            if t is not task and not t.done():
                                t.cancel()

                        task_idx = tasks.index(task)
                        level_name = task_names[task_idx]
                        if level_name == "pollinations_key":
                            self._level1_count += 1
                        elif level_name == "pollinations_free":
                            self._level2_count += 1
                        elif level_name == "cloudflare":
                            self._level3_count += 1
                        elif level_name == "local_fallback":
                            self._level0_count += 1

                        self._total_requests += 1
                        return await self._save_response(user_id, message, response, sys_prompt, use_cache, save_history)

            # All concurrent tasks failed — try Pollinations free as sequential fallback
            # (we didn't include it in concurrent tasks if we had paid keys)
            if not any(n == "pollinations_free" for n in task_names):
                logger.warning(f"All concurrent providers failed (route={route_type}), trying free API sequentially")
                response = await self._try_pollinations_free(
                    user_id, message, history, sys_prompt, temperature, max_tokens, model
                )
                if not response.error:
                    self._level2_count += 1
                    self._total_requests += 1
                    return await self._save_response(user_id, message, response, sys_prompt, use_cache, save_history)

            # ALL levels failed — collect error info from all tasks
            errors = []
            for task in done:
                try:
                    r = task.result()
                    if r.error:
                        errors.append(f"{r.provider}: {r.error_message}")
                except Exception as e:
                    errors.append(f"exception: {e}")

            # Cancel any remaining tasks
            for t in tasks:
                if not t.done():
                    t.cancel()

        except Exception as e:
            logger.error(f"Concurrent failover error: {e}")
            # Cancel all tasks on unexpected error
            for t in tasks:
                if not t.done():
                    t.cancel()

            # Try sequential fallback as emergency path
            try:
                response = await self._try_pollinations_free(
                    user_id, message, history, sys_prompt, temperature, max_tokens, model
                )
                if not response.error:
                    self._level2_count += 1
                    self._total_requests += 1
                    return await self._save_response(user_id, message, response, sys_prompt, use_cache, save_history)
            except Exception:
                pass

        # ── LAST RESORT: Static fallback ──
        self._static_count += 1
        self._total_fallbacks += 1
        logger.error(
            f"ALL LEVELS FAILED for route_type={route_type}. "
            f"Level0(local)={self._level0_count}, Level1={self._level1_count}, "
            f"Level2={self._level2_count}, Level3={self._level3_count}, Static={self._static_count}"
        )
        return AIResponse(
            text=random.choice(FALLBACK_RESPONSES),
            model="fallback",
            provider="static",
            tokens_used=0,
        )

    async def _save_response(
        self, user_id: int, message: str, response: AIResponse,
        sys_prompt: str, use_cache: bool, save_history: bool
    ) -> AIResponse:
        """Save response to history and cache."""
        if save_history and response.text:
            await add_chat_message(user_id, "user", message)
            await add_chat_message(user_id, "assistant", response.text)

            # Cache the response
            if use_cache and response.text:
                cache_key = self._make_cache_key(sys_prompt, message)
                await set_ai_cached(cache_key, message, response.text, response.model)

        return response

    async def _try_pollinations(self, user_id: int, message: str, history: list,
                                 sys_prompt: str, temperature: float, max_tokens: int,
                                 model: str) -> AIResponse:
        """Level 1: Try Pollinations with API key (KEY1 → KEY2 internally).
        Also tries model-level fallbacks.
        """
        messages = self._primary.format_messages(sys_prompt, history, message)

        # Try primary model (provider will try KEY1 → KEY2 internally)
        response = await self._primary.chat(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )

        # If primary failed, try fallback models
        if response.error:
            is_key_error = any(code in (response.error_message or "")
                              for code in ["All API keys depleted", "401", "402", "unavailable", "cooldown"])

            if is_key_error:
                fallback_models = [m for m in FALLBACK_MODELS
                                   if m != model and not self._primary._is_model_in_cooldown(m)][:1]
                if fallback_models:
                    logger.info(f"Key error, trying {len(fallback_models)} fallback model")
            else:
                fallback_models = [
                    m for m in ["mistral-small", "nova-fast", "gemma"]
                    if m != model and not self._primary._is_model_in_cooldown(m)
                ][:1]
                logger.info(f"Non-key error, trying fallback: {fallback_models}")

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

        return response

    async def _try_pollinations_free(self, user_id: int, message: str, history: list,
                                      sys_prompt: str, temperature: float, max_tokens: int,
                                      model: str) -> AIResponse:
        """Level 2: Try Pollinations FREE API (no auth, text.pollinations.ai)."""
        messages = self._primary.format_messages(sys_prompt, history, message)

        # Use simpler models for free API — they're more reliable
        free_models = ["openai", "mistral"]
        if model and model not in free_models:
            free_models.insert(0, model)

        for free_model in free_models[:2]:
            if self._primary._is_model_in_cooldown(free_model):
                continue
            result = await self._primary.chat_free(
                messages=messages,
                model=free_model,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            if not result.error and result.text:
                return result

        return AIResponse(
            text="",
            model=model or "openai",
            provider="pollinations-free",
            error="Free API failed for all models",
            error_message="Free API failed for all models",
        )

    async def _try_cloudflare(self, user_id: int, message: str, history: list,
                               sys_prompt: str, temperature: float, max_tokens: int) -> AIResponse:
        """Level 3: Try Cloudflare Workers AI (Mistral Small 3.1)."""
        if not self._cloudflare or not self._cloudflare._accounts:
            return AIResponse(
                text="",
                model="cloudflare",
                provider="cloudflare",
                error="Cloudflare not configured",
                error_message="Cloudflare not configured",
            )

        # Cloudflare has only one model — Mistral Small 3.1
        # It handles Russian well, good for all route types
        messages = self._primary.format_messages(sys_prompt, history, message)

        return await self._cloudflare.chat(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    async def _try_local(self, user_id: int, message: str, history: list,
                          sys_prompt: str, temperature: float, max_tokens: int) -> AIResponse:
        """Level 0: Try Local Model (Qwen3-4B).

        Uses ChatML format for Qwen3 with /no_think for fast responses.
        Primary for CHAT and COMMENT routes, fallback for FUNCTION routes.
        
        CONTEXT WINDOW BUDGET (8192 tokens, ~1.3 chars/token for Russian):
          - Output: 2048 tokens (MODEL_MAX_TOKENS)
          - Safety margin: 64 tokens
          - Available for input: 6080 tokens (~7904 chars)
          - System prompt (local_system_prompt v3): ~2340 chars (~1800 tokens)
          - History: 6 turns × 200 chars = ~1200 chars (~924 tokens)
          - User message: up to ~2000 chars (~1538 tokens)
          - Total: ~5540 chars (~4262 tokens) — fits in 6080 with margin!
        """
        if not self._local:
            return AIResponse(
                text="",
                model="local-qwen3-4b",
                provider="local",
                error="Local provider not initialized",
                error_message="Local provider not initialized",
            )

        if not config.ENABLE_LOCAL_MODEL:
            return AIResponse(
                text="",
                model="local-qwen3-4b",
                provider="local",
                error="Local model disabled (ENABLE_LOCAL_MODEL=false)",
                error_message="Local model disabled (ENABLE_LOCAL_MODEL=false)",
            )

        # Use EXPANDED system prompt for local model (v3.0 — ~1800 tokens).
        # With 8192 ctx, we have plenty of room for system prompt + history + user message.
        compact_prompt = persona.local_system_prompt

        # Build messages for local model using its own ChatML format
        messages = [{"role": "system", "content": compact_prompt}]

        # Add limited history for local model (saves context window)
        # With 8192 ctx and expanded prompt (~1800 tokens), we have ~4280 tokens left.
        # 6 history turns × ~200 chars × 1.3 chars/token = ~924 tokens
        # User message up to ~2000 chars = ~1538 tokens
        # Total: ~2462 tokens — well within 4280 budget.
        history_limit = min(config.MODEL_HISTORY_LIMIT, 6)
        limited_history = history[-history_limit:] if history else []
        for msg in limited_history:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            # Truncate long history messages to save context tokens
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content[:300]})

        # Truncate user message to prevent context overflow.
        # With 8192 ctx, allow up to ~2000 chars for user message.
        # The local_provider.chat() has its own safety truncation as well.
        truncated_message = message[:2000] if len(message) > 2000 else message
        if len(message) > 2000:
            logger.debug(f"Truncated user message for local model: {len(message)} → 2000 chars")
        messages.append({"role": "user", "content": truncated_message})

        return await self._local.chat(
            messages=messages,
            temperature=temperature,
            max_tokens=min(max_tokens, config.MODEL_MAX_TOKENS),
        )

    async def analyze_image(
        self,
        user_id: int,
        image_url: str = "",
        image_base64: str = "",
        prompt: str = "",
        extra_context: str = "",
    ) -> AIResponse:
        """
        Analyze an image with 3-level vision failover.

        Level 1: Pollinations vision (with key)
        Level 2: Pollinations vision (free API)
        Level 3: Cloudflare vision (Mistral Small 3.1 with image_url)
        Last resort: Static fallback
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
            recent = history[-6:]
            for msg in recent:
                role = "Пользователь" if msg.get("role") == "user" else "Ася"
                content = msg.get("content", "")[:100]
                if content:
                    context_summary += f"{role}: {content}\n"

        if context_summary:
            sys_prompt += f"\n\nКонтекст недавней беседы:\n{context_summary}"

        # ── LEVEL 1: Pollinations vision with key ──
        response = await self._primary.analyze_image(
            image_url=image_url,
            image_base64=image_base64,
            prompt=prompt,
            model="openai",  # Primary vision model
            system_prompt=sys_prompt,
            max_tokens=800,
            temperature=0.7,
        )

        if not response.error:
            self._level1_count += 1
            # Save to history
            if response.text:
                await add_chat_message(user_id, "user", f"[Фото] {prompt[:97]}")
                await add_chat_message(user_id, "assistant", response.text)
            return response

        # ── LEVEL 2: Pollinations FREE vision ──
        logger.warning(f"Vision Level 1 failed, trying free API: {response.error_message}")
        response = await self._primary.analyze_image_free(
            image_url=image_url,
            image_base64=image_base64,
            prompt=prompt,
            model="openai",
            system_prompt=sys_prompt,
            max_tokens=800,
            temperature=0.7,
        )

        if not response.error:
            self._level2_count += 1
            if response.text:
                await add_chat_message(user_id, "user", f"[Фото] {prompt[:97]}")
                await add_chat_message(user_id, "assistant", response.text)
            return response

        # ── LEVEL 3: Cloudflare vision (Mistral Small 3.1 with image_url) ──
        logger.warning(f"Vision Level 2 failed, trying Cloudflare: {response.error_message}")
        if self._cloudflare and self._cloudflare._accounts:
            response = await self._cloudflare.analyze_image(
                image_url=image_url,
                image_base64=image_base64,
                prompt=prompt,
                system_prompt=sys_prompt,
                max_tokens=800,
                temperature=0.7,
            )

            if not response.error:
                self._level3_count += 1
                if response.text:
                    await add_chat_message(user_id, "user", f"[Фото] {prompt[:97]}")
                    await add_chat_message(user_id, "assistant", response.text)
                return response

        # ── LAST RESORT: Static fallback ──
        self._static_count += 1
        self._total_fallbacks += 1
        logger.error(f"ALL vision levels failed: {response.error_message}")
        return AIResponse(
            text="Ой, не получилось разглядеть фото 😅 Попробуй ещё раз!",
            model="fallback",
            provider="static",
            error="All vision providers failed",
            error_message=response.error_message or "All vision providers failed",
        )

    async def decode_vin(
        self,
        user_id: int,
        vin_code: str,
        extra_context: str = "",
    ) -> AIResponse:
        """
        Decode a VIN code or body number for vehicle information.
        FUNCTION route — best quality models for accuracy.
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

        # VIN is a function — use best quality models
        return await self.chat(
            user_id=user_id,
            message=f"Расшифруй VIN: {vin_clean}",
            system_prompt=persona.system_prompt + persona.vin_prompt_suffix,
            model="openai-large",  # Best model for VIN
            temperature=0.3,  # More precise for VIN
            extra_context=vin_context,
            route_type="function",
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
        3-level failover: Pollinations key → free → Cloudflare.
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
                f"\n\n⛔ КРИТИЧЕСКИ ВАЖНО — ЛИМИТ СИМВОЛОВ ⛔\n"
                f"Это пост С ФОТО. Telegram обрезает подписи на 1024 символе.\n"
                f"Подпись 'Автор @asiaexp_bot / @sochiautoparts / #sochiautoparts' занимает ~55 символов.\n"
                f"Значит твой текст — СТРОГО НЕ БОЛЕЕ {content_limit} символов.\n"
                f"ЕСЛИ ТЫ НАПИШЕШЬ БОЛЬШЕ — ПОСТ ОБРЕЖЕТСЯ НА ПОЛУСЛОВЕ! Это выглядит ужасно.\n"
                f"Пиши КОМПАКТНО и ЁМКО: 500-800 символов оптимально. Абсолютный максимум 950.\n"
                f"НЕ ПИШИ длинных вступлений. Сразу к делу.\n"
                f"Подпись в конце ОБЯЗАТЕЛЬНА — никогда не обрезай её."
            )
        else:
            limit_instruction = (
                f"\n\nЛИМИТ СИМВОЛОВ:\n"
                f"Это текстовый пост БЕЗ медиа — разрешён только для ОЧЕНЬ интересного контента!\n"
                f"Такой пост без фото допускается потому что контент не помещается в 1024 символа,\n"
                f"но он достаточно интересен чтобы публиковать без фото.\n"
                f"Telegram лимит: 4096 символов весь пост. Пиши содержательно и компактно.\n"
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

        # ── Determine max_tokens based on has_media ──
        # Media posts: 1024 char limit → ~400 tokens is plenty
        # Text-only posts: 4096 char limit → ~1500 tokens for rich content
        post_max_tokens = 600 if has_media else 1500

        # ── LEVEL 1: Pollinations with key ──
        post_model = model or "openai-large"
        response = await self._primary.chat(
            messages=messages,
            model=post_model,
            temperature=0.8,
            max_tokens=post_max_tokens,
        )

        # If primary model failed, try a few quality fallback models
        if response.error:
            for fallback in ["gpt-5.5", "deepseek", "openai"]:
                if fallback == post_model:
                    continue
                if self._primary._is_model_in_cooldown(fallback):
                    continue
                logger.info(f"Channel post: trying fallback model {fallback}")
                response = await self._primary.chat(
                    messages=messages,
                    model=fallback,
                    temperature=0.8,
                    max_tokens=post_max_tokens,
                )
                if not response.error:
                    break

        # ── LEVEL 2: Pollinations free API ──
        if response.error:
            logger.warning(f"Channel post Level 1 failed, trying free API")
            for free_model in ["openai", "mistral", "openai-large"]:
                result = await self._primary.chat_free(
                    messages=messages,
                    model=free_model,
                    temperature=0.8,
                    max_tokens=post_max_tokens,
                )
                if not result.error and result.text:
                    response = result
                    break

        # ── LEVEL 3: Cloudflare ──
        if response.error and self._cloudflare and self._cloudflare._accounts:
            logger.warning(f"Channel post Level 2 failed, trying Cloudflare")
            response = await self._cloudflare.chat(
                messages=messages,
                temperature=0.8,
                max_tokens=post_max_tokens,
            )

        response = self._finalize_channel_post(response, has_media)
        return response

    @staticmethod
    def _router_smart_truncate(text: str, max_len: int) -> str:
        """Smart truncation for AI router — cuts at sentence/paragraph boundary.
        
        Same logic as channel.py's _smart_truncate to avoid mid-word cuts.
        """
        if len(text) <= max_len:
            return text
        
        target = max_len - 3
        if target < 50:
            return text[:target] + "..."
        
        search_zone = text[:target + 1]
        
        # 1. Paragraph break
        last_para = search_zone.rfind("\n\n")
        if last_para > target * 0.5:
            return text[:last_para].rstrip() + "..."
        
        # 2. Sentence end
        sentence_end_chars = ['. ', '! ', '? ', '… ', '.\n', '!\n', '?\n', '…\n']
        best_sentence_end = -1
        for end_char in sentence_end_chars:
            pos = search_zone.rfind(end_char)
            if pos > best_sentence_end and pos > target * 0.5:
                best_sentence_end = pos + len(end_char) - 1
        
        if best_sentence_end > target * 0.5:
            return text[:best_sentence_end + 1].rstrip() + "..."
        
        # 3. Newline
        last_newline = search_zone.rfind("\n")
        if last_newline > target * 0.5:
            return text[:last_newline].rstrip() + "..."
        
        # 4. Space
        last_space = search_zone.rfind(" ")
        if last_space > target * 0.5:
            return text[:last_space].rstrip() + "..."
        
        # 5. Hard cut
        return text[:target].rstrip() + "..."



    def _finalize_channel_post(self, response: AIResponse, has_media: bool) -> AIResponse:
        """Finalize channel post: add footer, enforce limits."""
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

            # Enforce character limit (use smart truncation from channel.py)
            footer = "\n\nАвтор @asiaexp_bot\n@sochiautoparts\n#sochiautoparts"
            if has_media and len(text) > config.TELEGRAM_CAPTION_LIMIT:
                for foot_part in ["\n\nАвтор @asiaexp_bot", "\n@sochiautoparts", "\n#sochiautoparts"]:
                    text = text.replace(foot_part, "")
                text = text.rstrip()
                max_content = config.TELEGRAM_CAPTION_LIMIT - len(footer)
                if len(text) > max_content:
                    # Smart truncation — find last sentence/paragraph boundary
                    text = self._router_smart_truncate(text, max_content)
                text += footer
            elif not has_media and len(text) > config.TELEGRAM_TEXT_LIMIT:
                for foot_part in ["\n\nАвтор @asiaexp_bot", "\n@sochiautoparts", "\n#sochiautoparts"]:
                    text = text.replace(foot_part, "")
                text = text.rstrip()
                max_content = config.TELEGRAM_TEXT_LIMIT - len(footer)
                if len(text) > max_content:
                    text = self._router_smart_truncate(text, max_content)
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
        """Generate a car diagnosis response. FUNCTION route — best quality models."""
        from bot.asya import build_diagnostic_context

        extra_context = build_diagnostic_context(symptoms)
        if car_info:
            extra_context = f"Информация об авто: {car_info}\n{extra_context}"

        return await self.chat(
            user_id=user_id,
            message=symptoms,
            system_prompt=persona.system_prompt + persona.diagnostic_prompt_suffix,
            model=model or "gpt-5.5",  # Complex task — use strong model
            temperature=0.5,
            extra_context=extra_context,
            route_type="function",
        )

    async def find_spare_part(
        self,
        user_id: int,
        article: str,
        part_info: str = "",
        model: str = "",
    ) -> AIResponse:
        """Generate a spare part search response. FUNCTION route — best quality models."""
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
            route_type="function",
        )

    async def comment(
        self,
        user_id: int,
        message: str,
        system_prompt: str = "",
        extra_context: str = "",
    ) -> AIResponse:
        """Generate a comment in another group. Uses fast/cheap models.
        3-level failover: Pollinations key → free → Cloudflare → Static.
        """
        return await self.chat(
            user_id=user_id,
            message=message,
            system_prompt=system_prompt or persona.system_prompt,
            temperature=0.7,
            max_tokens=200,  # Short comments
            use_cache=False,
            save_history=False,
            extra_context=extra_context,
            route_type="comment",
        )

    async def generate_comment(
        self,
        prompt: str,
        max_tokens: int = 100,
    ) -> AIResponse:
        """Generate a short comment for group posts. LOCAL MODEL ONLY.
        
        Per user requirement: comments in groups/chats MUST use local model only.
        No external API calls for commenting — this protects privacy and reduces costs.
        
        If the local model is unavailable, returns an error response.
        NO cloud fallback for comments — this is a hard requirement from the user.
        """
        system_prompt = (
            "Ты Ася — автоэксперт, главред канала @sochiautoparts. "
            "Пиши коротко, живо, как живой человек. "
            "Без markdown, без буллетов, без политики. "
            "Твой ответ — ТОЛЬКО текст комментария. "
            "Максимум 300 символов."
        )
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]
        
        # ── LOCAL MODEL ONLY — no cloud fallback for group comments ──
        # User explicitly requires: "комментирование в чатах и группах только через локальную модель"
        if self._local and self._local._model_loaded and self._local._llm is not None:
            try:
                response = await self._local.chat(
                    messages=messages,
                    temperature=0.8,
                    max_tokens=max_tokens,
                )
                if not response.error and response.text and len(response.text.strip()) > 5:
                    logger.info("Comment generated by LOCAL model (Qwen3-4B)")
                    # Clean the response
                    text = response.text.strip()
                    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
                    text = re.sub(r'\*(.+?)\*', r'\1', text)
                    text = re.sub(r'<[^>]+>', '', text)
                    response.text = text
                    return response
            except Exception as e:
                logger.debug(f"Local model comment failed: {e}")
        else:
            logger.info("Local model not available for comment — skipping (no cloud API for comments)")
        
        # Local model unavailable or failed — return error instead of using cloud
        logger.warning("Local model unavailable for comment — NOT using cloud (user requirement)")
        return AIResponse(
            text="",
            error="Local model unavailable for comments — cloud APIs blocked by user requirement",
            provider="local-only",
            model="qwen3-4b",
            error_message="Local model unavailable for comments — cloud APIs blocked by user requirement",
        )

    @staticmethod
    def _clean_ai_response(text: str) -> str:
        """Clean AI response artifacts (think tags, markdown, structured output, etc.)."""
        if not text:
            return ""

        # Block structured YAML/MIDI/JSON output from text-to-music models
        import re
        structured_patterns = [
            r'^title:\s*.+?\n(duration|key|notation|pitch|velocity|tempo|bpm):',
            r'^---\s*\n.*?(title|duration|notation|pitch|velocity):',
            r'pitch,\s*time,\s*duration,\s*velocity',
        ]
        for pattern in structured_patterns:
            if re.search(pattern, text, re.DOTALL | re.IGNORECASE):
                return ""

        # Strip think tags (Qwen3, reasoning models)
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

    def get_status(self) -> Dict[str, str]:
        """Get current status of all providers."""
        status = {}
        # Local model
        if self._local:
            status["local"] = self._local.get_status()
        else:
            status["local"] = "not_initialized"
        # Pollinations
        status["pollinations_keys"] = self._primary._get_key_status_summary()
        status["pollinations_free"] = "available" if self._primary._is_free_api_available() else "cooldown"
        # Cloudflare
        if self._cloudflare and self._cloudflare._accounts:
            status["cloudflare"] = self._cloudflare.get_status()
        else:
            status["cloudflare"] = "not_configured"
        # Stats
        status["stats"] = (
            f"L0(local)={self._level0_count} "
            f"L1(poll-key)={self._level1_count} "
            f"L2(poll-free)={self._level2_count} "
            f"L3(cloudflare)={self._level3_count} "
            f"static={self._static_count}"
        )
        return status


# ── Global instance ────────────────────────────────────────────────────────────

ai_router = AIRouter()
