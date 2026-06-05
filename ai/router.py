"""
AI Router v4.0 — SMART ROUTING with DUAL-KEY FAILOVER.
Qwen3-4B primary for simple chat, Pollinations (dual-key) for complex tasks + vision.

Route strategy (v4.0):
  CHAT route_type (user chats) → LOCAL-FIRST: Local → Cloud (KEY1→KEY2) → Fallback
  FUNCTION route_type (posts, VIN, diagnostics, parts) → CLOUD-FIRST: Cloud (KEY1→KEY2) → Local → Fallback
  COMMENT route_type (comments in other groups) → LOCAL-ONLY: Local → Static fallback
  VISION tasks (photos) → Pollinations vision only (KEY1→KEY2)

  If Pollinations is down or both keys depleted → ALL routes fall back to local model
"""

import hashlib
import logging
import random
import time
from typing import Optional, List, Dict
from datetime import datetime
from zoneinfo import ZoneInfo

from ai.providers.base import BaseAIProvider, AIResponse
from ai.providers.pollinations_provider import (
    PollinationsProvider, POLLINATIONS_MODELS,
    CHAT_MODELS, REASONING_MODELS, VISION_MODELS,
    CONTENT_MODELS, SEARCH_MODELS, IMAGE_MODELS, FALLBACK_MODELS,
)
from bot.config import config, persona
from bot.database import get_ai_cached, set_ai_cached, get_chat_history, add_chat_message

# Conditional import — LlamaCpp local
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

# ── Task complexity detection for LOCAL-FIRST routing ──
# Keywords that indicate a SIMPLE task (local model can handle)
_SIMPLE_TASK_KEYWORDS = [
    "привет", "как дела", "пока", "спасибо", "ок", "ладно",
    "что делаешь", "скучно", "доброе утро", "добрый день", "добрый вечер",
    "спокойной ночи", "как настроен", "чем занята",
    "кто ты", "что ты", "сколько тебе", "где ты живёшь",
    "хаха", "ахах", "лол", "прикольно", "круто", "класс",
    "да", "нет", "норм", "супер", "окей", "ага", "угу",
]

# Keywords that indicate a COMPLEX task (cloud model needed)
_COMPLEX_TASK_KEYWORDS = [
    # VIN & auto diagnostics
    "vin", "вин", "расшифруй", "декодир", "пробей",
    "диагност", "ремонт", "не заводит", "стучит", "горит",
    "ошибка", "чек", "check", "код ошибки", "obd",
    # Parts search
    "запчаст", "деталь", "артикул", "купить", "найти запчас",
    "подобрать", "оригинал", "аналог", "oem",
    # Detailed/complex requests
    "подробно", "расскажи подробно", "объясни подробно",
    "сравни", "проанализир", "рассчитай", "посчитай",
    # Search and products
    "найди", "поищи", "ищи", "где купить", "купить",
    "ссылк", "заказать", "цена", "стоимость", "сколько",
    # Tech & science
    "как работает", "принцип действия", "устройство",
    "почему", "зачем", "из-за чего", "в чём разница",
]


def _classify_task_complexity(message: str) -> str:
    """Classify task complexity to route to local or cloud model.
    
    Returns:
        "simple" — local model can handle (saves cloud balance)
        "complex" — cloud model recommended
        "cloud_only" — must use cloud (vision, etc.)
    """
    msg_lower = message.lower().strip()
    
    # Long messages are complex
    if len(message) > 300:
        return "complex"
    
    # Check for complex task keywords
    for kw in _COMPLEX_TASK_KEYWORDS:
        if kw in msg_lower:
            return "complex"
    
    # Check for simple task keywords
    simple_count = sum(1 for kw in _SIMPLE_TASK_KEYWORDS if kw in msg_lower)
    if simple_count > 0 and len(message) < 100:
        return "simple"
    
    # Default: simple for short messages, complex for longer
    if len(message) < 150:
        return "simple"
    
    return "complex"


class AIRouter:
    """Routes AI requests to the best available provider with fallback.

    v3.0 SMART ROUTING strategy:
    - Chat (user conversations) → LOCAL-FIRST: Local → Cloud → Fallback
    - Function (posts, VIN, diagnostics, parts) → CLOUD-FIRST: Cloud → Local → Fallback
    - Comment (comments in other groups) → LOCAL-ONLY: Local → Static fallback
    - Vision (photos) → Pollinations cloud only
    """

    def __init__(self):
        self.providers: List[BaseAIProvider] = []
        self._primary: Optional[PollinationsProvider] = None
        self._local: Optional[LlamaCppProvider] = None
        self._total_fallbacks: int = 0
        self._local_requests: int = 0
        self._cloud_requests: int = 0
        self._local_primary_count: int = 0

    async def initialize(self) -> None:
        """Initialize all providers: LlamaCpp PRIMARY + Pollinations COMPLEX."""
        # ── 1. LlamaCpp — LOCAL PRIMARY (for simple tasks, saves balance!) ──
        if config.ENABLE_LOCAL_MODEL and config.MODEL_PATH and _LLAMA_CPP_AVAILABLE and LlamaCppProvider is not None:
            try:
                self._local = LlamaCppProvider(
                    model_path=config.MODEL_PATH,
                    timeout=65.0,
                    model_config={
                        "n_ctx": max(config.MODEL_N_CTX, 4096),  # Minimum 4096!
                        "n_threads": config.MODEL_N_THREADS,
                        "n_gpu_layers": 0,
                        "verbose": False,
                        "use_mmap": True,
                        "use_mlock": False,
                    },
                    gen_config={
                        "max_tokens": max(config.MODEL_MAX_TOKENS, 384),  # Minimum 384!
                        "temperature": 0.82,
                        "top_p": 0.92,
                        "top_k": 50,
                        "repeat_penalty": 1.12,
                    },
                )
                await self._local.init()
                logger.info("LlamaCppProvider initialized as LOCAL PRIMARY (saves cloud balance!)")
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

        # ── 2. Pollinations — CLOUD FOR COMPLEX TASKS ──
        pollinations = PollinationsProvider()
        self.providers = [pollinations]
        self._primary = pollinations
        logger.info(
            f"AI Router: Pollinations initialized for COMPLEX TASKS "
            f"({len(POLLINATIONS_MODELS)} models: "
            f"{len(CHAT_MODELS)} chat, {len(VISION_MODELS)} vision, "
            f"{len(CONTENT_MODELS)} content, {len(SEARCH_MODELS)} search)"
        )

        # Log status
        local_status = "not_installed" if not _LLAMA_CPP_AVAILABLE else ("disabled" if not config.ENABLE_LOCAL_MODEL else ("active" if self._local and self._local.is_available() else "unavailable"))
        pollinations_status = "active" if self._primary else "unavailable"
        model_name = self._local._model_name if self._local and self._local._loaded else "none"

        logger.info(
            f"AI Router v4.0 DUAL-KEY SMART ROUTING initialized: "
            f"local={local_status} (chat=LOCAL-FIRST, model={model_name}, n_ctx={max(config.MODEL_N_CTX, 4096)}), "
            f"pollinations={pollinations_status} (function=CLOUD-FIRST, comment=LOCAL-ONLY, vision=cloud, "
            f"dual-key=KEY1+KEY2, {len(POLLINATIONS_MODELS)} models)"
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
        route_type: str = "chat",
    ) -> AIResponse:
        """
        Send a chat message through the AI router.

        v3.0 SMART ROUTING via route_type:
        - "chat" (default): LOCAL-FIRST — Local → Cloud → Fallback. Saves balance!
        - "function": CLOUD-FIRST — Cloud → Local → Fallback. Better quality for public content.
        - "comment": LOCAL-ONLY — Local → Static fallback. No cloud waste on casual comments.
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

        # ── Route based on route_type ──
        if route_type == "comment":
            # LOCAL-ONLY: Local → Static fallback. No cloud waste on comments.
            response = await self._try_local_only(user_id, message, history, sys_prompt, temperature, max_tokens, model)
        elif route_type == "function":
            # CLOUD-FIRST: Cloud → Local → Fallback. Better quality for public content.
            response = await self._try_cloud_first(user_id, message, history, sys_prompt, temperature, max_tokens, model)
        else:
            # CHAT (default): LOCAL-FIRST — Local → Cloud → Fallback. Saves balance!
            complexity = _classify_task_complexity(message)
            if complexity == "simple" and self._local and self._local.is_available():
                response = await self._try_local_first(user_id, message, history, sys_prompt, temperature, max_tokens, model)
            else:
                response = await self._try_cloud_first(user_id, message, history, sys_prompt, temperature, max_tokens, model)

        # Save to history
        if save_history and not response.error and response.text:
            await add_chat_message(user_id, "user", message)
            await add_chat_message(user_id, "assistant", response.text)

            # Cache the response
            if use_cache and response.text:
                cache_key = self._make_cache_key(sys_prompt, message)
                await set_ai_cached(cache_key, message, response.text, response.model)

        return response

    async def _try_local_only(self, user_id: int, message: str, history: list,
                              sys_prompt: str, temperature: float, max_tokens: int,
                              model: str) -> AIResponse:
        """LOCAL-ONLY route: Local → Static fallback.
        Used for comments in other groups — no cloud waste on casual comments.
        """
        if self._local and self._local.is_available():
            try:
                local_base = (
                    "Ты Ася — автоэксперт, редактор канала @sochiautoparts. "
                    "Пиши кратко, по делу. Без политики, без markdown. "
                    f"Сейчас {datetime.now(_MOSCOW_TZ).strftime('%d.%m.%Y %H:%M')} по Москве."
                )
                # Extract context from full prompt
                extra_context = ""
                if sys_prompt:
                    import re as _re
                    search_match = _re.search(r'Результаты поиска:.*', sys_prompt, _re.DOTALL)
                    if search_match:
                        extra_context += "\n\n" + search_match.group(0)[:400]
                    diag_match = _re.search(r'Категория проблемы:.*', sys_prompt, _re.DOTALL)
                    if diag_match:
                        extra_context += "\n\n" + diag_match.group(0)[:300]
                    vin_match = _re.search(r'VIN-код.*', sys_prompt, _re.DOTALL)
                    if vin_match:
                        extra_context += "\n\n" + vin_match.group(0)[:300]
                local_system_prompt = local_base + extra_context
                local_messages = [{"role": "system", "content": local_system_prompt}]
                if history:
                    for msg in history[-4:]:
                        role = msg.get("role", "user")
                        content = msg.get("content", "")
                        if role in ("user", "assistant") and content:
                            if len(content) > 200:
                                content = content[:200] + "..."
                            local_messages.append({"role": role, "content": content})
                local_messages.append({"role": "user", "content": message[:800]})

                local_response = await self._local.chat(
                    messages=local_messages,
                    model="",
                    temperature=0.82,
                    max_tokens=200,  # Short responses for comments
                )
                if not local_response.error and local_response.text:
                    self._local_requests += 1
                    text = self._clean_ai_response(local_response.text)
                    if text:
                        logger.info(f"LOCAL-ONLY: comment handled by local model")
                        return AIResponse(
                            text=text,
                            model=local_response.model,
                            provider=local_response.provider,
                            tokens_used=local_response.tokens_used,
                        )
            except Exception as e:
                logger.warning(f"Local model error (comment): {e}")

        # Static fallback — no cloud for comments!
        self._total_fallbacks += 1
        logger.warning("Local model unavailable for comment, using static fallback")
        return AIResponse(
            text=random.choice(FALLBACK_RESPONSES),
            model="fallback",
            provider="static",
            tokens_used=0,
        )

    async def _try_local_first(self, user_id: int, message: str, history: list,
                                sys_prompt: str, temperature: float, max_tokens: int,
                                model: str) -> AIResponse:
        """LOCAL-FIRST route: Local → Pollinations → static fallback.
        
        v3: Local model now receives FULL context (web search, partner links, diagnostics, etc.)
        The sys_prompt already contains all enriched context from chat.py.
        """
        # ── 1. Try LOCAL model first (with FULL context!) ──
        if self._local and self._local.is_available():
            try:
                # Build local system prompt with FULL context from chat.py
                local_base = (
                    "Ты Ася — автоэксперт, редактор канала @sochiautoparts. "
                    "Пиши живо, с юмором, как автожурналист. "
                    "Без политики, без markdown. "
                    f"Сейчас {datetime.now(_MOSCOW_TZ).strftime('%d.%m.%Y %H:%M')} по Москве."
                )
                # Extract web search and partner context from the full system prompt
                extra_context = ""
                if sys_prompt:
                    # Extract web search results
                    import re as _re
                    search_match = _re.search(r'Результаты поиска:.*', sys_prompt, _re.DOTALL)
                    if search_match:
                        extra_context += "\n\n" + search_match.group(0)[:600]
                    # Extract partner links
                    partner_match = _re.search(r'Партнёрск.*?(?:естественно\.)', sys_prompt, _re.DOTALL)
                    if partner_match:
                        extra_context += "\n\n" + partner_match.group(0)[:400]
                    # Extract direct shop links
                    shop_match = _re.search(r'Прямые ссылки.*?(?:купить\!\))', sys_prompt, _re.DOTALL)
                    if shop_match:
                        extra_context += "\n\n" + shop_match.group(0)[:500]
                    # Extract diagnostic context
                    diag_match = _re.search(r'Категория проблемы:.*', sys_prompt, _re.DOTALL)
                    if diag_match:
                        extra_context += "\n\n" + diag_match.group(0)[:300]
                    # Extract VIN context
                    vin_match = _re.search(r'VIN-код.*', sys_prompt, _re.DOTALL)
                    if vin_match:
                        extra_context += "\n\n" + vin_match.group(0)[:300]
                local_system_prompt = local_base + extra_context
                local_messages = [{"role": "system", "content": local_system_prompt}]
                if history:
                    for msg in history[-6:]:
                        role = msg.get("role", "user")
                        content = msg.get("content", "")
                        if role in ("user", "assistant") and content:
                            if len(content) > 300:
                                content = content[:300] + "..."
                            local_messages.append({"role": role, "content": content})
                local_messages.append({"role": "user", "content": message[:1200]})

                local_response = await self._local.chat(
                    messages=local_messages,
                    model="",
                    temperature=0.82,
                    max_tokens=384,
                )
                if not local_response.error and local_response.text:
                    self._local_requests += 1
                    self._local_primary_count += 1
                    text = self._clean_ai_response(local_response.text)
                    if text:
                        logger.info(f"LOCAL PRIMARY: simple task handled by local model")
                        return AIResponse(
                            text=text,
                            model=local_response.model,
                            provider=local_response.provider,
                            tokens_used=local_response.tokens_used,
                        )
            except Exception as e:
                logger.warning(f"Local model error (simple task): {e}")

        # ── 2. Pollinations fallback ──
        return await self._try_pollinations(user_id, message, history, sys_prompt, temperature, max_tokens, model)

    async def _try_cloud_first(self, user_id: int, message: str, history: list,
                                sys_prompt: str, temperature: float, max_tokens: int,
                                model: str) -> AIResponse:
        """CLOUD-FIRST route: Pollinations → Local (with full context) → static fallback."""
        # ── 1. Try Pollinations first ──
        response = await self._try_pollinations(user_id, message, history, sys_prompt, temperature, max_tokens, model)
        if not response.error:
            return response

        # ── 2. LOCAL model fallback (with FULL context!) ──
        if self._local and self._local.is_available():
            logger.warning("Pollinations failed, falling back to LOCAL model (with full context)")
            try:
                # Build local system prompt with FULL context
                local_base = (
                    "Ты Ася — автоэксперт, редактор канала @sochiautoparts. "
                    "Пиши живо, с юмором, как автожурналист. "
                    "Без политики, без markdown. "
                    f"Сейчас {datetime.now(_MOSCOW_TZ).strftime('%d.%m.%Y %H:%M')} по Москве."
                )
                # Extract web search and partner context
                extra_context = ""
                if sys_prompt:
                    import re as _re
                    search_match = _re.search(r'Результаты поиска:.*', sys_prompt, _re.DOTALL)
                    if search_match:
                        extra_context += "\n\n" + search_match.group(0)[:600]
                    partner_match = _re.search(r'Партнёрск.*?(?:естественно\.)', sys_prompt, _re.DOTALL)
                    if partner_match:
                        extra_context += "\n\n" + partner_match.group(0)[:400]
                    shop_match = _re.search(r'Прямые ссылки.*?(?:купить\!\))', sys_prompt, _re.DOTALL)
                    if shop_match:
                        extra_context += "\n\n" + shop_match.group(0)[:500]
                    diag_match = _re.search(r'Категория проблемы:.*', sys_prompt, _re.DOTALL)
                    if diag_match:
                        extra_context += "\n\n" + diag_match.group(0)[:300]
                    vin_match = _re.search(r'VIN-код.*', sys_prompt, _re.DOTALL)
                    if vin_match:
                        extra_context += "\n\n" + vin_match.group(0)[:300]
                local_system_prompt = local_base + extra_context
                local_messages = [{"role": "system", "content": local_system_prompt}]
                if history:
                    for msg in history[-6:]:
                        role = msg.get("role", "user")
                        content = msg.get("content", "")
                        if role in ("user", "assistant") and content:
                            if len(content) > 300:
                                content = content[:300] + "..."
                            local_messages.append({"role": role, "content": content})
                local_messages.append({"role": "user", "content": message[:1200]})

                local_response = await self._local.chat(
                    messages=local_messages,
                    model="",
                    temperature=0.82,
                    max_tokens=384,
                )
                if not local_response.error and local_response.text:
                    self._local_requests += 1
                    text = self._clean_ai_response(local_response.text)
                    if text:
                        logger.info(f"LOCAL FALLBACK: cloud failed, local model handled it")
                        return AIResponse(
                            text=text,
                            model=local_response.model,
                            provider=local_response.provider,
                            tokens_used=local_response.tokens_used,
                        )
            except Exception as e:
                logger.warning(f"Local model fallback error: {e}")

        # ── 3. Static fallback ──
        self._total_fallbacks += 1
        logger.error("All AI providers unavailable! Using static fallback.")
        return AIResponse(
            text=random.choice(FALLBACK_RESPONSES),
            model="fallback",
            provider="static",
            tokens_used=0,
        )

    async def _try_pollinations(self, user_id: int, message: str, history: list,
                                 sys_prompt: str, temperature: float, max_tokens: int,
                                 model: str) -> AIResponse:
        """Try Pollinations with smart model selection and dual-key fallback.

        The provider handles KEY1 → KEY2 failover internally.
        Here we handle model-level fallback (try different models if one fails).
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
                # Both keys depleted — try a few different models with broader selection
                # Different models may have different balance pools
                fallback_models = [m for m in FALLBACK_MODELS
                                   if m != model and not self._primary._is_model_in_cooldown(m)][:3]
                if fallback_models:
                    logger.info(f"Key error, trying {len(fallback_models)} fallback models")
            else:
                # Other errors (timeout, server error) — try a few fallback models
                fallback_models = [
                    m for m in ["mistral-small", "deepseek-v4", "llama-3.3", "nova-fast",
                                "gemma", "qwen3-coder", "step-3.5-flash", "nova-micro"]
                    if m != model and not self._primary._is_model_in_cooldown(m)
                ][:3]
                logger.info(f"Non-key error, trying fallbacks: {fallback_models}")

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
                    self._cloud_requests += 1
                    break

        if not response.error:
            self._cloud_requests += 1

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
        FUNCTION route — CLOUD-FIRST for accuracy (cloud → local → fallback).
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

        # VIN is a function — CLOUD-FIRST route
        return await self.chat(
            user_id=user_id,
            message=f"Расшифруй VIN: {vin_clean}",
            system_prompt=persona.system_prompt + persona.vin_prompt_suffix,
            model="openai-large",  # Best model for VIN
            temperature=0.3,  # More precise for VIN
            extra_context=vin_context,
            route_type="function",  # CLOUD-FIRST
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
        CLOUD-FIRST — better quality for public content (cloud → local → fallback).
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

        # ── CLOUD-FIRST for channel posts (better quality for public content!) ──
        # Try cloud first, then local as fallback
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

        post_model = model or "openai-large"
        response = await self._primary.chat(
            messages=messages,
            model=post_model,
            temperature=0.8,
            max_tokens=1500,
        )

        # If cloud failed, try local model as fallback
        if response.error and self._local and self._local.is_available():
            logger.warning("Cloud failed for channel post, trying local model fallback")
            try:
                local_messages = [
                    {"role": "system", "content": "Ты Ася — автоэксперт, редактор канала @sochiautoparts. Пиши живо, с юмором. Без политики. Обязательно добавь подпись: Автор @asiaexp_bot / @sochiautoparts / #sochiautoparts"},
                    {"role": "user", "content": user_content[:1200]},
                ]
                local_response = await self._local.chat(
                    messages=local_messages,
                    model="",
                    temperature=0.8,
                    max_tokens=384,
                )
                if not local_response.error and local_response.text:
                    text = self._clean_ai_response(local_response.text)
                    if text and len(text) >= 100:
                        self._local_requests += 1
                        response = AIResponse(text=text, model=local_response.model, provider=local_response.provider, tokens_used=local_response.tokens_used)
            except Exception as e:
                logger.warning(f"Local model channel post fallback error: {e}")

        response = self._finalize_channel_post(response, has_media)
        return response

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
        """Generate a car diagnosis response. FUNCTION route — CLOUD-FIRST."""
        from bot.asya import build_diagnostic_context

        extra_context = build_diagnostic_context(symptoms)
        if car_info:
            extra_context = f"Информация об авто: {car_info}\n{extra_context}"

        return await self.chat(
            user_id=user_id,
            message=symptoms,
            system_prompt=persona.system_prompt + persona.diagnostic_prompt_suffix,
            model=model or "gpt-5.5",  # Complex task — use strong cloud model
            temperature=0.5,
            extra_context=extra_context,
            route_type="function",  # CLOUD-FIRST
        )

    async def find_spare_part(
        self,
        user_id: int,
        article: str,
        part_info: str = "",
        model: str = "",
    ) -> AIResponse:
        """Generate a spare part search response. FUNCTION route — CLOUD-FIRST."""
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
            route_type="function",  # CLOUD-FIRST
        )

    async def comment(
        self,
        user_id: int,
        message: str,
        system_prompt: str = "",
        extra_context: str = "",
    ) -> AIResponse:
        """Generate a comment in another group. LOCAL-ONLY route — no cloud waste.
        If local model fails, uses static fallback.
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
            route_type="comment",  # LOCAL-ONLY
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
