"""
Pollinations AI Provider — OpenAI-compatible API at gen.pollinations.ai
Multi-model support with vision, reasoning, chat, image, and audio capabilities.
"""

import httpx
import json
import base64
import logging
import time
from typing import Optional, List, Dict

from ai.providers.base import BaseAIProvider, AIResponse
from bot.config import config

logger = logging.getLogger("asya.ai.pollinations")

# ── Model Categories ──────────────────────────────────────────────────────────

# Chat models — for general conversation (text only)
# All models tested and confirmed working with our API key (June 2026)
CHAT_MODELS = [
    "openai",              # GPT-5.4, 400K context, tools, text+image
    "openai-fast",         # GPT-5 nano, 400K context, tools — fastest OpenAI
    "openai-large",        # GPT-5.4 reasoning, 400K context, tools
    "gpt-5.4-mini",        # GPT-5.4 mini, 400K context, tools
    "gpt-5.5",             # GPT-5.5, 1M context — newest flagship (TESTED OK)
    "mistral",             # Mistral Small, 128K context, tools, text+image
    "mistral-large",       # Mistral Large, 256K context, tools, reasoning
    "mistral-4",           # Mistral 4, 262K context, tools, reasoning
    "mistral-small",       # Mistral Small 3.2, 24B, tools, text+image — fast (TESTED OK)
    "mistral-small-3.2",   # Mistral Small 3.2 (alias), 24B (TESTED OK)
    "deepseek",            # DeepSeek V4, 1M context, tools, reasoning
    "deepseek-pro",        # DeepSeek V4 Pro, 1M context — stronger reasoning (TESTED OK)
    "deepseek-v4",         # DeepSeek V4 Flash, fast variant (TESTED OK)
    "qwen-coder",          # Qwen Coder, 262K context, tools
    "qwen3-coder",         # Qwen3 Coder 30B, tools — new generation (TESTED OK)
    "qwen-large",          # Qwen Large, 1M context, reasoning, vision
    "qwen-vision",         # Qwen Vision, 131K context, tools, text+image
    "qwen-vision-pro",     # Qwen Vision Pro, 262K context, reasoning, text+image
    "qwen-safety",         # Qwen Safety, content moderation
    "llama",               # Llama 3.3 70B, 131K context, tools
    "llama-3.3",           # Llama 3.3 70B (explicit), 131K context (TESTED OK)
    "llama-scout",         # Llama 4 Scout, 328K context, tools, text+image
    "llama-4-scout",       # Llama 4 Scout (alias), 328K context (TESTED OK)
    "nova",                # Nova 2, 1M context, tools, reasoning, text+image
    "nova-fast",           # Nova Micro, 128K context, tools — very fast
    "nova-2",              # Nova 2 Lite, fast, Russian OK (TESTED OK)
    "grok",                # Grok 4, 262K context, tools, text+image
    "grok-large",          # Grok 4 Large, 262K context, tools, reasoning, text+image
    "grok-4.3",            # Grok 4.3, 262K context — latest Grok (TESTED OK)
    "perplexity",          # Sonar Pro, 200K context — web search
    "perplexity-fast",     # Sonar, 128K context — fast web search
    "perplexity-deep",     # Sonar deep search (TESTED OK)
    "perplexity-reasoning",# Sonar Reasoning Pro, web search + reasoning (TESTED OK)
    "gemma",               # Gemma 4, 262K context, tools, reasoning, text+image
    "glm",                 # GLM 5, 198K context, tools, reasoning — Russian OK
    "minimax",             # MiniMax M2, 200K context, tools, reasoning
    "minimax-m3",          # MiniMax M3, 1M context, tools, reasoning, text+image
    "kimi",                # Kimi K2.5, 262K context, tools, reasoning
    "kimi-k2.6",           # Kimi K2.6, 262K context — latest Kimi (TESTED OK)
    "step-3.5-flash",      # Step 3.5 Flash, 262K context, tools, reasoning
    "step-flash",          # Step Flash, 256K context, tools, reasoning, text+image
    "polly",               # Polly, reasoning model with tools
]

# Reasoning models — for complex analysis and diagnostics
REASONING_MODELS = [
    "gpt-5.5",             # GPT-5.5, 1M context — strongest reasoning (TESTED OK)
    "openai-large",        # GPT-5.4 reasoning
    "deepseek-pro",        # DeepSeek V4 Pro — stronger reasoning (TESTED OK)
    "deepseek-v4",         # DeepSeek V4 Flash — fast reasoning (TESTED OK)
    "qwen-large",          # Qwen Large, reasoning + vision (TESTED OK)
    "qwen3-coder",         # Qwen3 Coder, strong reasoning (TESTED OK)
    "deepseek",            # DeepSeek V4, reasoning
    "mistral-4",           # Mistral 4, reasoning
    "step-flash",          # Step Flash, reasoning + image (TESTED OK)
    "polly",               # Polly, reasoning (TESTED OK)
    "grok-4.3",            # Grok 4.3, latest reasoning (TESTED OK)
    "grok-large",          # Grok Large, reasoning (TESTED OK)
    "minimax-m3",          # MiniMax M3, reasoning (TESTED OK)
    "mistral-large",       # Mistral Large, reasoning (TESTED OK)
    "perplexity-reasoning",# Sonar Reasoning Pro, web + reasoning (TESTED OK)
    "llama-3.3",           # Llama 3.3 70B, reasoning (TESTED OK)
    "nova-2",              # Nova 2 Lite, fast reasoning (TESTED OK)
]

# Vision models — can understand images
VISION_MODELS = [
    "openai",              # Primary vision model
    "openai-fast",         # Fast vision
    "gpt-5.5",             # GPT-5.5 vision (TESTED OK)
    "mistral",             # Vision capable
    "mistral-small",       # Vision capable (TESTED OK)
    "qwen-vision",         # Qwen Vision, 131K context, tools, text+image (TESTED OK)
    "qwen-vision-pro",     # Qwen Vision Pro, 262K context, reasoning, text+image (TESTED OK)
    "qwen-large",          # Qwen Large, reasoning + vision (TESTED OK)
    "llama-scout",         # Vision capable (TESTED OK)
    "nova",                # Vision capable
    "minimax-m3",          # Vision capable (TESTED OK)
    "gemma",               # Vision capable
    "grok",                # Vision capable
    "grok-4.3",            # Vision capable (TESTED OK)
    "step-flash",          # Vision capable (TESTED OK)
]

# Content creation models — for generating channel posts
CONTENT_MODELS = [
    "openai-large",        # Best quality for content
    "gpt-5.5",             # GPT-5.5 — best overall (TESTED OK)
    "qwen-large",          # Good for detailed content (TESTED OK)
    "deepseek-pro",        # Strong reasoning for content (TESTED OK)
    "deepseek-v4",         # Fast content generation (TESTED OK)
    "deepseek",            # Good analysis
    "mistral-4",           # Good writing
    "step-flash",          # Good for content (TESTED OK)
    "polly",               # Slow but thorough (TESTED OK)
    "mistral-large",       # High-quality writing (TESTED OK)
    "minimax-m3",          # Detailed content (TESTED OK)
    "qwen3-coder",         # Good structured content (TESTED OK)
    "llama-3.3",           # Good Russian writing (TESTED OK)
    "nova-2",              # Fast content (TESTED OK)
]

# Perplexity models — web-search augmented
SEARCH_MODELS = [
    "perplexity",          # Sonar Pro (TESTED OK)
    "perplexity-fast",     # Sonar (TESTED OK)
    "perplexity-deep",     # Sonar deep search (TESTED OK)
    "perplexity-reasoning",# Sonar Reasoning Pro, web + reasoning (TESTED OK)
]

# Image generation models
IMAGE_MODELS = [
    "flux",                # Flux — text→image
    "gptimage",            # GPT Image — text→image
    "gptimage-large",      # GPT Image Large — text→image
    "kontext",             # Kontext — text+image→image
    "zimage",              # ZImage — text→image
    "nova-canvas",         # Nova Canvas — text+image→image
    "klein",               # Klein — text→image
]

# Audio / transcription models
AUDIO_MODELS = [
    "whisper",             # Whisper — audio→text
    "universal-2",         # Universal 2 — audio→text
    "universal-3-pro",     # Universal 3 Pro — audio→text
]

# All available models
POLLINATIONS_MODELS = (
    CHAT_MODELS + REASONING_MODELS + VISION_MODELS +
    SEARCH_MODELS + IMAGE_MODELS + AUDIO_MODELS
)
# Remove duplicates while preserving order
POLLINATIONS_MODELS = list(dict.fromkeys(POLLINATIONS_MODELS))

DEFAULT_MODEL = "openai"
FALLBACK_MODELS = [
    "mistral-4", "deepseek", "nova-fast", "grok", "grok-large",
    "grok-4.3", "minimax", "llama-scout", "gemma", "kimi", "kimi-k2.6", "glm",
    "mistral-small", "step-flash", "polly", "mistral-large", "qwen-large",
    "minimax-m3", "step-3.5-flash", "gpt-5.4-mini", "deepseek-pro",
    "deepseek-v4", "qwen3-coder", "llama-3.3", "nova-2", "mistral-small-3.2",
    "gpt-5.5", "perplexity-deep", "perplexity-reasoning",
]

# Track model failures for circuit breaking
_model_failures: Dict[str, float] = {}
_FAILURE_COOLDOWN = 300  # 5 minutes

# Free tier models — known to work without API key/balance
# These models are typically available on Pollinations free tier (no auth needed)
# The free tier rotates — models may become available/unavailable
FREE_TIER_MODELS = [
    "openai", "openai-fast", "mistral-small", "llama-3.3", "deepseek-v4",
    "gemma", "nova-fast", "qwen3-coder", "mistral-small-3.2",
    "step-3.5-flash", "polly", "llama-scout", "llama-4-scout",
    "nova-2", "qwen-coder", "kimi", "glm",
]

# Priority models to try first (fast + reliable)
PRIORITY_FREE_MODELS = [
    "openai-fast", "mistral-small", "llama-3.3", "deepseek-v4",
    "nova-fast", "gemma", "qwen3-coder", "step-3.5-flash",
]


class PollinationsProvider(BaseAIProvider):
    """Pollinations AI provider — OpenAI-compatible API with multi-model support.

    Supports automatic free-tier fallback when API key balance is depleted.
    When 402 (Insufficient Balance) is received, switches to free tier mode
    (no API key) and retries. Periodically tries the API key again.
    """

    def __init__(self):
        super().__init__(
            name="pollinations",
            api_key=config.POLLINATIONS_API_KEY,
            base_url=config.POLLINATIONS_BASE_URL,
        )
        # Free tier state
        self._balance_depleted_at: Optional[float] = None
        self._BALANCE_RETRY_INTERVAL = 600  # Try API key again after 10 min
        # API unavailable state — when ALL models fail (even free tier)
        self._api_unavailable_at: Optional[float] = None
        self._API_UNAVAILABLE_COOLDOWN = 120  # Wait 2 min before retrying after total failure
        self._consecutive_auth_failures: int = 0  # Track repeated 401/402 failures
        self._MAX_AUTH_FAILURES = 3  # After this many, enter unavailable mode

    def _should_use_api_key(self) -> bool:
        """Check if we should try using the API key (not balance-depleted)."""
        if not self.api_key:
            return False
        if self._balance_depleted_at is None:
            return True
        # Try API key again after retry interval
        if time.time() - self._balance_depleted_at > self._BALANCE_RETRY_INTERVAL:
            logger.info("Retrying with API key after balance cooldown...")
            self._balance_depleted_at = None
            self._consecutive_auth_failures = 0
            return True
        return False

    def _is_api_available(self) -> bool:
        """Check if the API is available (not in total unavailable mode)."""
        if self._api_unavailable_at is None:
            return True
        if time.time() - self._api_unavailable_at > self._API_UNAVAILABLE_COOLDOWN:
            logger.info("Retrying API after unavailable cooldown...")
            self._api_unavailable_at = None
            self._consecutive_auth_failures = 0
            return True
        return False

    def _mark_api_unavailable(self):
        """Mark the API as totally unavailable (all models failing)."""
        self._api_unavailable_at = time.time()
        logger.warning(f"API marked as unavailable for {self._API_UNAVAILABLE_COOLDOWN}s — all models failing")

    async def chat(
        self,
        messages: List[Dict[str, str]],
        model: str = "",
        temperature: float = 0.7,
        max_tokens: int = 2048,
        **kwargs,
    ) -> AIResponse:
        """Send a chat completion request to Pollinations API.

        Strategy:
        1. If API is totally unavailable, return error immediately
        2. Try with API key first (if balance available)
        3. On 402 (Insufficient Balance), switch to free tier (no API key)
        4. On 401 on free tier, try other free models quickly (max 3 attempts)
        5. If all free models also fail with auth errors, mark API as unavailable
        """
        model = model or DEFAULT_MODEL

        # Fast fail: if API is totally unavailable, don't waste time
        if not self._is_api_available():
            return AIResponse(
                text="",
                model=model,
                provider=self.name,
                error=True,
                error_message="API temporarily unavailable (cooldown)",
            )

        # Check if model is in cooldown from recent failures
        if self._is_model_in_cooldown(model):
            alt = self._get_available_model()
            if alt:
                logger.info(f"Model {model} in cooldown, using {alt}")
                model = alt

        # Determine if we should use API key or free tier
        use_api_key = self._should_use_api_key()

        # Build headers based on auth mode
        headers = {"Content-Type": "application/json"}
        if use_api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        if kwargs.get("top_p"):
            payload["top_p"] = kwargs["top_p"]
        if kwargs.get("frequency_penalty"):
            payload["frequency_penalty"] = kwargs["frequency_penalty"]
        if kwargs.get("presence_penalty"):
            payload["presence_penalty"] = kwargs["presence_penalty"]

        try:
            async with httpx.AsyncClient(timeout=90.0) as client:
                start_time = time.time()
                url = f"{self.base_url}/v1/chat/completions"
                response = await client.post(url, headers=headers, json=payload)
                elapsed = time.time() - start_time

                if response.status_code == 200:
                    data = response.json()
                    text = ""
                    choices = data.get("choices", [])
                    if choices:
                        msg = choices[0].get("message", {})
                        text = msg.get("content", "")
                        if not text:
                            text = msg.get("reasoning_content", "")

                    usage = data.get("usage", {})
                    tokens_used = usage.get("total_tokens", 0)
                    actual_model = data.get("model", model)
                    tier = "paid" if use_api_key else "free"

                    # Success! Reset failure counters
                    self._consecutive_auth_failures = 0
                    if self._balance_depleted_at and use_api_key:
                        logger.info("API key balance restored!")
                        self._balance_depleted_at = None

                    logger.info(
                        f"Pollinations response ({tier}): model={actual_model}, "
                        f"tokens={tokens_used}, time={elapsed:.1f}s, "
                        f"length={len(text)}"
                    )

                    return AIResponse(
                        text=text,
                        model=actual_model,
                        provider=self.name,
                        tokens_used=tokens_used,
                    )

                elif response.status_code == 402 and use_api_key:
                    # Balance depleted — switch to free tier and retry
                    logger.warning("Pollinations balance depleted, switching to free tier")
                    self._balance_depleted_at = time.time()
                    self._consecutive_auth_failures += 1

                    # Try free tier models quickly — max 3 attempts
                    free_models_to_try = [m for m in PRIORITY_FREE_MODELS if m != model][:3]
                    for free_model in free_models_to_try:
                        if self._is_model_in_cooldown(free_model):
                            continue
                        free_headers = {"Content-Type": "application/json"}
                        payload["model"] = free_model

                        start_time = time.time()
                        response = await client.post(url, headers=free_headers, json=payload)
                        elapsed = time.time() - start_time

                        if response.status_code == 200:
                            data = response.json()
                            text = ""
                            choices = data.get("choices", [])
                            if choices:
                                msg = choices[0].get("message", {})
                                text = msg.get("content", "")
                                if not text:
                                    text = msg.get("reasoning_content", "")

                            usage = data.get("usage", {})
                            tokens_used = usage.get("total_tokens", 0)
                            actual_model = data.get("model", free_model)

                            # Success on free tier!
                            self._consecutive_auth_failures = 0

                            logger.info(
                                f"Pollinations free tier response: model={actual_model}, "
                                f"tokens={tokens_used}, time={elapsed:.1f}s, "
                                f"length={len(text)}"
                            )

                            return AIResponse(
                                text=text,
                                model=actual_model,
                                provider=self.name,
                                tokens_used=tokens_used,
                            )
                        elif response.status_code in (401, 402):
                            # Auth error on free tier too — model needs auth
                            _model_failures[free_model] = time.time()
                            self._consecutive_auth_failures += 1
                            logger.warning(f"Free tier model {free_model} also returned {response.status_code}")
                            continue
                        else:
                            # Other error — try next model
                            _model_failures[free_model] = time.time()
                            continue

                    # All free tier models failed with auth errors
                    if self._consecutive_auth_failures >= self._MAX_AUTH_FAILURES:
                        self._mark_api_unavailable()

                    return AIResponse(
                        text="",
                        model=model,
                        provider=self.name,
                        error=True,
                        error_message=f"API unavailable (balance depleted, free tier also failing). Retry in {self._API_UNAVAILABLE_COOLDOWN}s",
                    )

                elif response.status_code == 401 and not use_api_key:
                    # Free tier model requires auth — try another free model
                    _model_failures[model] = time.time()
                    self._consecutive_auth_failures += 1
                    logger.warning(f"Free tier model {model} requires auth, trying alternatives")

                    # Try 2 more free models quickly
                    alt_models = [m for m in PRIORITY_FREE_MODELS if m != model and not self._is_model_in_cooldown(m)][:2]
                    for alt_model in alt_models:
                        payload["model"] = alt_model
                        start_time = time.time()
                        response = await client.post(url, headers=headers, json=payload)
                        elapsed = time.time() - start_time

                        if response.status_code == 200:
                            data = response.json()
                            text = ""
                            choices = data.get("choices", [])
                            if choices:
                                msg = choices[0].get("message", {})
                                text = msg.get("content", "")
                                if not text:
                                    text = msg.get("reasoning_content", "")

                            usage = data.get("usage", {})
                            tokens_used = usage.get("total_tokens", 0)
                            actual_model = data.get("model", alt_model)

                            self._consecutive_auth_failures = 0

                            logger.info(
                                f"Pollinations free tier response (alt): model={actual_model}, "
                                f"tokens={tokens_used}, time={elapsed:.1f}s, "
                                f"length={len(text)}"
                            )

                            return AIResponse(
                                text=text,
                                model=actual_model,
                                provider=self.name,
                                tokens_used=tokens_used,
                            )
                        else:
                            _model_failures[alt_model] = time.time()
                            self._consecutive_auth_failures += 1
                            continue

                    if self._consecutive_auth_failures >= self._MAX_AUTH_FAILURES:
                        self._mark_api_unavailable()

                    return AIResponse(
                        text="",
                        model=model,
                        provider=self.name,
                        error=True,
                        error_message="All free tier models require authentication",
                    )

                else:
                    error_text = response.text[:500]
                    logger.error(
                        f"Pollinations error: status={response.status_code}, "
                        f"model={model}, error={error_text}"
                    )
                    _model_failures[model] = time.time()

                    return AIResponse(
                        text="",
                        model=model,
                        provider=self.name,
                        error=True,
                        error_message=f"HTTP {response.status_code}: {error_text}",
                    )

        except httpx.TimeoutException:
            logger.error(f"Pollinations timeout: model={model}")
            _model_failures[model] = time.time()
            return AIResponse(
                text="",
                model=model,
                provider=self.name,
                error=True,
                error_message="Request timed out",
            )

        except Exception as e:
            logger.error(f"Pollinations exception: model={model}, error={e}")
            _model_failures[model] = time.time()
            return AIResponse(
                text="",
                model=model,
                provider=self.name,
                error=True,
                error_message=str(e),
            )

    async def generate_image(self, prompt: str, model: str = "flux") -> Optional[bytes]:
        """
        Generate an image using Pollinations image API.
        Returns image bytes or None on failure.
        """
        try:
            url = f"{self.base_url}/v1/images/generations"
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            }
            payload = {
                "model": model,
                "prompt": prompt,
                "size": "1344x768",  # Good for Telegram channel posts
            }

            async with httpx.AsyncClient(timeout=120.0) as client:
                response = await client.post(url, headers=headers, json=payload)

                if response.status_code == 200:
                    data = response.json()
                    for item in data.get("data", []):
                        if item.get("b64_json"):
                            return base64.b64decode(item["b64_json"])
                        elif item.get("url"):
                            img_resp = await client.get(item["url"])
                            if img_resp.status_code == 200:
                                return img_resp.content
                else:
                    logger.error(f"Image generation error: {response.status_code} {response.text[:300]}")

        except Exception as e:
            logger.error(f"Image generation exception: {e}")

        return None

    async def analyze_image(
        self,
        image_url: str = "",
        image_base64: str = "",
        prompt: str = "Опиши подробно что ты видишь на этом изображении.",
        model: str = "openai",
        system_prompt: str = "",
        max_tokens: int = 600,
        temperature: float = 0.7,
    ) -> AIResponse:
        """
        Analyze an image using vision-capable models.
        Supports both URL and base64-encoded images.
        """
        # Build the content array for vision
        content = [
            {"type": "text", "text": prompt}
        ]

        if image_url:
            content.append({
                "type": "image_url",
                "image_url": {"url": image_url}
            })
        elif image_base64:
            # Determine media type from base64 header or default to jpeg
            media_type = "image/jpeg"
            if image_base64.startswith("data:"):
                header, image_base64 = image_base64.split(",", 1)
                if "png" in header:
                    media_type = "image/png"
                elif "webp" in header:
                    media_type = "image/webp"
                elif "gif" in header:
                    media_type = "image/gif"

            content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:{media_type};base64,{image_base64}"
                }
            })

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})

        messages.append({"role": "user", "content": content})

        # Try primary vision model, then fallbacks
        vision_models_to_try = [model]
        for fallback in VISION_MODELS:
            if fallback != model and not self._is_model_in_cooldown(fallback):
                vision_models_to_try.append(fallback)

        for vision_model in vision_models_to_try[:5]:  # Try up to 5 models
            try:
                result = await self.chat(
                    messages=messages,
                    model=vision_model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                if not result.error and result.text:
                    return result
                logger.warning(f"Vision model {vision_model} failed, trying next...")
            except Exception as e:
                logger.error(f"Vision error with {vision_model}: {e}")
                continue

        return AIResponse(
            text="",
            model=model,
            provider=self.name,
            error=True,
            error_message="All vision models failed",
        )

    async def is_available(self) -> bool:
        """Check if Pollinations API is reachable."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    f"{self.base_url}/v1/models",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
                return response.status_code == 200
        except Exception:
            return False

    def _is_model_in_cooldown(self, model: str) -> bool:
        """Check if a model has recently failed and is in cooldown."""
        if model not in _model_failures:
            return False
        return time.time() - _model_failures[model] < _FAILURE_COOLDOWN

    def _get_available_model(self) -> Optional[str]:
        """Get an available model that's not in cooldown."""
        if not self._is_model_in_cooldown(DEFAULT_MODEL):
            return DEFAULT_MODEL
        for model in FALLBACK_MODELS:
            if not self._is_model_in_cooldown(model):
                return model
        if _model_failures:
            oldest = min(_model_failures, key=_model_failures.get)
            return oldest
        return DEFAULT_MODEL

    def get_model_list(self) -> List[str]:
        """Return list of available models."""
        return POLLINATIONS_MODELS.copy()
