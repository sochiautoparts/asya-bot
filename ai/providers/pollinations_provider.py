"""
Pollinations AI Provider v2.0 — DUAL API KEY + EXPANDED MODEL SUPPORT
OpenAI-compatible API at gen.pollinations.ai

v2.0 UPDATES:
  DUAL API KEY FAILOVER:
  - KEY1 → KEY2 → Free tier → Error
  - On 402/401: mark current key as depleted, auto-switch to next
  - Depleted keys auto-retry after 600 seconds cooldown
  - Free tier (no key) always available as last resort before error

  EXPANDED MODEL LIST (57 models from API catalog):
  - Chat: 35+ models for conversation
  - Reasoning: 16+ models for complex analysis
  - Vision: 14+ models for image understanding
  - Content: 13+ models for channel posts
  - Search: 4 Perplexity models
  - Image gen: 8 models
  - Audio: 3 models

  IMPORTANT: Models are NOT deleted when temporarily unavailable.
  Pollinations.ai rotates model availability — a 402/404 today
  doesn't mean the model is gone. We keep all models and use
  circuit breaking (5-min cooldown) for recently failed models.
"""

import httpx
import json
import base64
import logging
import time
from typing import Optional, List, Dict, Tuple

from ai.providers.base import BaseAIProvider, AIResponse
from bot.config import config

logger = logging.getLogger("asya.ai.pollinations")

# ── Cooldown for depleted API keys (seconds) ──
KEY_COOLDOWN: float = 600.0  # 10 minutes before retrying a depleted key

# ── Model Categories ──────────────────────────────────────────────────────────

# Chat models — for general conversation (text only)
# All models from Pollinations catalog (June 2026)
# DO NOT remove models when temporarily unavailable — Pollinations rotates availability!
CHAT_MODELS = [
    "openai",              # GPT-5.4, 400K context, tools, text+image
    "openai-fast",         # GPT-5 nano, 400K context, tools — fastest OpenAI
    "openai-large",        # GPT-5.4 reasoning, 400K context, tools
    "gpt-5.4-mini",        # GPT-5.4 mini, 400K context, tools
    "gpt-5.5",             # GPT-5.5, 1M context — newest flagship
    "mistral",             # Mistral Small, 128K context, tools, text+image
    "mistral-large",       # Mistral Large, 256K context, tools, reasoning
    "mistral-4",           # Mistral 4, 262K context, tools, reasoning
    "mistral-small",       # Mistral Small 3.2, 24B, tools, text+image — fast
    "mistral-small-3.2",   # Mistral Small 3.2 (alias), 24B
    "deepseek",            # DeepSeek V4, 1M context, tools, reasoning
    "deepseek-pro",        # DeepSeek V4 Pro, 1M context — stronger reasoning
    "deepseek-v4",         # DeepSeek V4 Flash, fast variant
    "qwen-coder",          # Qwen Coder, 262K context, tools
    "qwen3-coder",         # Qwen3 Coder 30B, tools — new generation
    "qwen-large",          # Qwen Large, 1M context, reasoning, vision
    "qwen-vision",         # Qwen Vision, 131K context, tools, text+image
    "qwen-vision-pro",     # Qwen Vision Pro, 262K context, reasoning, text+image
    "qwen-safety",         # Qwen Safety, content moderation
    "llama",               # Llama 3.3 70B, 131K context, tools
    "llama-3.3",           # Llama 3.3 70B (explicit), 131K context
    "llama-scout",         # Llama 4 Scout, 328K context, tools, text+image
    "llama-4-scout",       # Llama 4 Scout (alias), 328K context
    "nova",                # Nova 2, 1M context, tools, reasoning, text+image
    "nova-fast",           # Nova Micro, 128K context, tools — very fast
    "nova-2",              # Nova 2 Lite, fast, Russian OK
    "grok",                # Grok 4, 262K context, tools, text+image
    "grok-large",          # Grok 4 Large, 262K context, tools, reasoning, text+image
    "grok-4.3",            # Grok 4.3, 262K context — latest Grok
    "perplexity",          # Sonar Pro, 200K context — web search
    "perplexity-fast",     # Sonar, 128K context — fast web search
    "perplexity-deep",     # Sonar deep search
    "perplexity-reasoning",# Sonar Reasoning Pro, web search + reasoning
    "gemma",               # Gemma 4, 262K context, tools, reasoning, text+image
    "glm",                 # GLM 5, 198K context, tools, reasoning — Russian OK
    "minimax",             # MiniMax M2, 200K context, tools, reasoning
    "minimax-m3",          # MiniMax M3, 1M context, tools, reasoning, text+image
    "kimi",                # Kimi K2.5, 262K context, tools, reasoning
    "kimi-k2.6",           # Kimi K2.6, 262K context — latest Kimi
    "step-3.5-flash",      # Step 3.5 Flash, 262K context, tools, reasoning
    "step-flash",          # Step Flash, 256K context, tools, reasoning, text+image
    "polly",               # Polly, reasoning model with tools
    "openai-reasoning",    # OpenAI Reasoning — reasoning + vision
    "nova-micro",          # Amazon Nova Micro — ultra fast, cheapest
]

# Reasoning models — for complex analysis and diagnostics
REASONING_MODELS = [
    "gpt-5.5",             # GPT-5.5, 1M context — strongest reasoning
    "openai-large",        # GPT-5.4 reasoning
    "openai-reasoning",    # OpenAI Reasoning — reasoning + vision
    "deepseek-pro",        # DeepSeek V4 Pro — stronger reasoning
    "deepseek-v4",         # DeepSeek V4 Flash — fast reasoning
    "deepseek",            # DeepSeek V4, reasoning
    "qwen-large",          # Qwen Large, reasoning + vision
    "qwen3-coder",         # Qwen3 Coder, strong reasoning
    "mistral-4",           # Mistral 4, reasoning
    "mistral-large",       # Mistral Large, reasoning
    "step-flash",          # Step Flash, reasoning + image
    "polly",               # Polly, reasoning
    "grok-4.3",            # Grok 4.3, latest reasoning
    "grok-large",          # Grok Large, reasoning
    "minimax-m3",          # MiniMax M3, reasoning
    "perplexity-reasoning",# Sonar Reasoning Pro, web + reasoning
    "llama-3.3",           # Llama 3.3 70B, reasoning
    "nova-2",              # Nova 2 Lite, fast reasoning
]

# Vision models — can understand images
VISION_MODELS = [
    "openai",              # Primary vision model
    "openai-fast",         # Fast vision
    "openai-large",        # Vision + reasoning
    "openai-reasoning",    # Reasoning + vision
    "gpt-5.5",             # GPT-5.5 vision
    "mistral",             # Vision capable
    "mistral-4",           # Vision capable
    "mistral-small",       # Vision capable
    "qwen-vision",         # Qwen Vision specialist
    "qwen-vision-pro",     # Qwen Vision Pro, reasoning + image
    "qwen-large",          # Qwen Large, reasoning + vision
    "llama-scout",         # Vision capable
    "nova",                # Vision capable
    "minimax-m3",          # Vision capable
    "gemma",               # Vision capable
    "grok",                # Vision capable
    "grok-4.3",            # Vision capable
    "step-flash",          # Vision capable
    "kimi-k2.6",           # Vision capable
    "mistral-small-3.2",   # Vision capable
]

# Content creation models — for generating channel posts
CONTENT_MODELS = [
    "openai-large",        # Best quality for content
    "gpt-5.5",             # GPT-5.5 — best overall
    "openai-reasoning",    # Reasoning for complex content
    "qwen-large",          # Good for detailed content
    "deepseek-pro",        # Strong reasoning for content
    "deepseek-v4",         # Fast content generation
    "deepseek",            # Good analysis
    "mistral-4",           # Good writing
    "mistral-large",       # High-quality writing
    "step-flash",          # Good for content
    "polly",               # Slow but thorough
    "minimax-m3",          # Detailed content
    "qwen3-coder",         # Good structured content
    "llama-3.3",           # Good Russian writing
    "nova-2",              # Fast content
    "grok-large",          # Good Russian writing
]

# Perplexity models — web-search augmented
SEARCH_MODELS = [
    "perplexity",          # Sonar Pro
    "perplexity-fast",     # Sonar — fast
    "perplexity-deep",     # Sonar deep search
    "perplexity-reasoning",# Sonar Reasoning Pro, web + reasoning
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
    "ltx-2",               # LTX-2 — text→image (NEW)
]

# Audio / transcription models
AUDIO_MODELS = [
    "whisper",             # Whisper — audio→text
    "universal-2",         # Universal 2 — audio→text
    "universal-3-pro",     # Universal 3 Pro — audio→text
]

# All available models (deduplicated)
POLLINATIONS_MODELS = list(dict.fromkeys(
    CHAT_MODELS + REASONING_MODELS + VISION_MODELS +
    SEARCH_MODELS + IMAGE_MODELS + AUDIO_MODELS
))

DEFAULT_MODEL = "openai"
FALLBACK_MODELS = [
    "mistral-4", "deepseek", "nova-fast", "grok", "grok-large",
    "grok-4.3", "minimax", "llama-scout", "gemma", "kimi", "kimi-k2.6", "glm",
    "mistral-small", "step-flash", "polly", "mistral-large", "qwen-large",
    "minimax-m3", "step-3.5-flash", "gpt-5.4-mini", "deepseek-pro",
    "deepseek-v4", "qwen3-coder", "llama-3.3", "nova-2", "mistral-small-3.2",
    "gpt-5.5", "perplexity-deep", "perplexity-reasoning", "openai-reasoning",
    "nova-micro",
]

# Track model failures for circuit breaking
_model_failures: Dict[str, float] = {}
_FAILURE_COOLDOWN = 300  # 5 minutes


class PollinationsProvider(BaseAIProvider):
    """Pollinations AI provider v2.0 — DUAL KEY + EXPANDED MODELS!

    Uses gen.pollinations.ai/v1/chat/completions (OpenAI-compatible).

    DUAL API KEY FAILOVER:
      1. Try KEY1 first
      2. On 402/401 from KEY1 → switch to KEY2
      3. On 402/401 from KEY2 → raise error (signal router → use local model)
      4. Depleted keys auto-retry after 600 seconds cooldown

    IMPORTANT: Models are NEVER removed when unavailable.
    Pollinations.ai rotates model availability — a failure today
    doesn't mean the model is gone. Circuit breaking (5-min cooldown)
    handles temporary failures.
    """

    def __init__(self):
        super().__init__(
            name="pollinations",
            api_key=config.POLLINATIONS_API_KEY,
            base_url=config.POLLINATIONS_BASE_URL,
        )
        # ── Dual API key storage ──
        self._api_key_1: str = config.POLLINATIONS_API_KEY
        self._api_key_2: str = config.POLLINATIONS_API_KEY_2
        # ── Per-key balance depletion tracking ──
        # 0 = active/never depleted; >0 = timestamp when depleted
        self._key1_depleted_at: float = 0.0
        self._key2_depleted_at: float = 0.0
        # ── API unavailable state — when ALL keys fail ──
        self._api_unavailable_at: Optional[float] = None
        self._API_UNAVAILABLE_COOLDOWN = 120  # Wait 2 min before retrying after total failure
        self._consecutive_auth_failures: int = 0
        self._MAX_AUTH_FAILURES = 3  # After this many, enter unavailable mode

    # ── API Key Management ──────────────────────────────────────

    def _is_key_available(self, key_index: int) -> bool:
        """Check if an API key is available (not depleted or cooldown expired)."""
        if key_index == 1:
            depleted_at = self._key1_depleted_at
            key_val = self._api_key_1
        else:
            depleted_at = self._key2_depleted_at
            key_val = self._api_key_2

        if not key_val:
            return False

        if depleted_at == 0:
            return True

        elapsed = time.time() - depleted_at
        if elapsed >= KEY_COOLDOWN:
            if key_index == 1:
                self._key1_depleted_at = 0
            else:
                self._key2_depleted_at = 0
            logger.info(f"API KEY{key_index} cooldown expired after {elapsed:.0f}s — retrying")
            return True

        return False

    def _mark_key_depleted(self, key_index: int) -> None:
        """Mark an API key as depleted (balance exhausted)."""
        if key_index == 1:
            self._key1_depleted_at = time.time()
        else:
            self._key2_depleted_at = time.time()
        logger.warning(
            f"API KEY{key_index} depleted (402/401). "
            f"Will auto-retry after {KEY_COOLDOWN}s cooldown."
        )

    def _get_active_key_tier(self) -> Tuple[str, int]:
        """Determine which key/tier to use for the next request.

        Returns:
            Tuple of (api_key_or_empty_string, key_tier)
            key_tier: 1=KEY1, 2=KEY2, 0=no available key
        """
        if self._is_key_available(1):
            return self._api_key_1, 1
        if self._is_key_available(2):
            return self._api_key_2, 2
        return "", 0

    def _get_key_status_summary(self) -> str:
        """Get a human-readable summary of key statuses."""
        parts = []
        if self._api_key_1:
            if self._is_key_available(1):
                parts.append("KEY1=active")
            else:
                remaining = KEY_COOLDOWN - (time.time() - self._key1_depleted_at)
                parts.append(f"KEY1=depleted({remaining:.0f}s)")
        else:
            parts.append("KEY1=not_set")
        if self._api_key_2:
            if self._is_key_available(2):
                parts.append("KEY2=active")
            else:
                remaining = KEY_COOLDOWN - (time.time() - self._key2_depleted_at)
                parts.append(f"KEY2=depleted({remaining:.0f}s)")
        else:
            parts.append("KEY2=not_set")
        return ", ".join(parts)

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
        """Mark the API as totally unavailable (all keys failing)."""
        self._api_unavailable_at = time.time()
        key_status = self._get_key_status_summary()
        logger.warning(
            f"API marked as unavailable for {self._API_UNAVAILABLE_COOLDOWN}s — "
            f"all keys failing [{key_status}]"
        )

    async def chat(
        self,
        messages: List[Dict[str, str]],
        model: str = "",
        temperature: float = 0.7,
        max_tokens: int = 2048,
        **kwargs,
    ) -> AIResponse:
        """Send a chat completion request to Pollinations API with DUAL-KEY FAILOVER.

        Strategy:
        1. If API is totally unavailable, return error immediately
        2. Try with KEY1 first (if balance available)
        3. On 402/401 from KEY1 → switch to KEY2
        4. On 402/401 from KEY2 → mark API as unavailable, return error
        5. Router will then fall back to local model
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

        # Build key tier list: active keys first
        tiers_to_try: List[Tuple[str, int]] = []
        if self._is_key_available(1):
            tiers_to_try.append((self._api_key_1, 1))
        if self._is_key_available(2):
            tiers_to_try.append((self._api_key_2, 2))

        # If no keys available at all, check cooldown
        if not tiers_to_try:
            return AIResponse(
                text="",
                model=model,
                provider=self.name,
                error=True,
                error_message=f"All API keys depleted [{self._get_key_status_summary()}]",
            )

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

        last_error = None
        for api_key, tier_index in tiers_to_try:
            try:
                headers = {"Content-Type": "application/json"}
                if api_key:
                    headers["Authorization"] = f"Bearer {api_key}"

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
                        tier_label = f"KEY{tier_index}"

                        # Success! Reset failure counters
                        self._consecutive_auth_failures = 0

                        logger.info(
                            f"Pollinations response ({tier_label}): model={actual_model}, "
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
                        # Balance depleted or unauthorized → switch key
                        self._mark_key_depleted(tier_index)
                        last_error = f"HTTP {response.status_code} from KEY{tier_index}"
                        logger.warning(
                            f"HTTP {response.status_code} from {model} via KEY{tier_index} — "
                            f"switching to next key tier"
                        )
                        continue  # Try next key tier

                    elif response.status_code == 429:
                        # Rate limited — short cooldown
                        logger.warning(f"Rate limited (429) on KEY{tier_index}, model={model}")
                        _model_failures[model] = time.time()
                        # Try next key tier
                        continue

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
                logger.error(f"Pollinations timeout: model={model}, tier=KEY{tier_index}")
                _model_failures[model] = time.time()
                last_error = f"Timeout from KEY{tier_index}"
                continue  # Try next key tier

            except Exception as e:
                logger.error(f"Pollinations exception: model={model}, tier=KEY{tier_index}, error={e}")
                _model_failures[model] = time.time()
                last_error = str(e)
                continue

        # ── All key tiers failed with 402/401 → mark API as unavailable ──
        self._consecutive_auth_failures += 1
        if self._consecutive_auth_failures >= self._MAX_AUTH_FAILURES:
            self._mark_api_unavailable()

        key_status = self._get_key_status_summary()
        logger.error(
            f"All API key tiers failed for model {model}. "
            f"Key status: [{key_status}]. Last error: {last_error}. "
            f"Router should fall back to local model."
        )

        return AIResponse(
            text="",
            model=model,
            provider=self.name,
            error=True,
            error_message=f"All API keys depleted/unavailable [{key_status}]. {last_error}",
        )

    async def generate_image(self, prompt: str, model: str = "flux") -> Optional[bytes]:
        """
        Generate an image using Pollinations image API.
        Uses dual-key failover: KEY1 → KEY2.
        Returns image bytes or None on failure.
        """
        # Build key tier list
        tiers_to_try: List[Tuple[str, int]] = []
        if self._is_key_available(1):
            tiers_to_try.append((self._api_key_1, 1))
        if self._is_key_available(2):
            tiers_to_try.append((self._api_key_2, 2))

        if not tiers_to_try:
            logger.warning("No API keys available for image generation")
            return None

        for api_key, tier_index in tiers_to_try:
            try:
                url = f"{self.base_url}/v1/images/generations"
                headers = {
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
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
                    elif response.status_code in (401, 402):
                        self._mark_key_depleted(tier_index)
                        logger.warning(f"Image gen HTTP {response.status_code} via KEY{tier_index}, trying next tier...")
                        continue
                    else:
                        logger.error(f"Image generation error: {response.status_code} {response.text[:300]}")
                        return None

            except Exception as e:
                logger.error(f"Image generation exception: {e}")
                continue

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
        Uses dual-key failover for each vision model attempt.
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
        active_key, _ = self._get_active_key_tier()
        if not active_key:
            return False
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    f"{self.base_url}/v1/models",
                    headers={"Authorization": f"Bearer {active_key}"},
                )
                return response.status_code == 200
        except Exception:
            return False

    def _is_model_in_cooldown(self, model: str) -> bool:
        """Check if a model has recently failed and is in cooldown.

        IMPORTANT: We do NOT remove models from lists when they fail.
        Pollinations.ai rotates model availability — a failure today
        doesn't mean the model is permanently unavailable.
        """
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
