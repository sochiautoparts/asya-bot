"""Pollinations AI Provider v5.0 — DUAL API KEY + FREE API FALLBACK + 60+ MODEL SUPPORT
OpenAI-compatible API at gen.pollinations.ai

v5.0 UPDATES:
  DUAL API KEY FAILOVER + FREE API FALLBACK:
  - KEY1 (config.POLLINATIONS_API_KEY) -> KEY2 (config.POLLINATIONS_API_KEY_2) -> FREE API
  - On 402/401: mark current key as depleted, auto-switch to next
  - Depleted keys auto-retry after 600 seconds cooldown
  - FREE API: text.pollinations.ai / image.pollinations.ai WITHOUT Authorization
  - Free API is the LAST resort before router fallback to Cloudflare
  - NO hardcoded keys — all from environment/config

  EXPANDED MODEL LIST (60+ models from API catalog, June 2026):
  - Chat: 43+ models for conversation
  - Reasoning: 17+ models for complex analysis
  - Vision: 22+ models for image understanding
  - Content: 15+ models for channel posts
  - Search: 4 Perplexity models
  - Image gen: 8 models
  - Audio: 3 models

  IMPORTANT: Models are NEVER deleted when temporarily unavailable.
  Pollinations.ai rotates model availability -- a 402/404 today
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

# No hardcoded API keys — all from config/environment

# ── Model Categories ──────────────────────────────────────────────────────────

# Chat models — for general conversation (43 models)
# All models from Pollinations catalog (June 2026)
# DO NOT remove models when temporarily unavailable — Pollinations rotates availability!
CHAT_MODELS = [
    "openai",              # GPT-5.4 Nano — PRIMARY, fast, 400K context, tools, text+image
    "gpt-5.4-mini",        # GPT-5.4 mini, 400K context, tools — balanced
    "mistral",             # Mistral Small, 128K context, tools, text+image — fast vision
    "mistral-4",           # Mistral 4, 262K context, tools, reasoning — better vision+reasoning
    "deepseek",            # DeepSeek V4, 1M context, tools, reasoning — fast reasoning
    "grok",                # Grok 4, 262K context, tools, text+image — good multilingual
    "gemma",               # Gemma 4, 262K context, tools, reasoning, text+image — fast MoE
    "llama",               # Llama 3.3 70B, 131K context, tools — strong base
    "llama-scout",         # Llama 4 Scout, 328K context, tools, text+image — vision+long context
    "nova-fast",           # Nova Micro, 128K context, tools — ultra fast
    "nova",                # Nova 2, 1M context, tools, reasoning, text+image — good quality
    "glm",                 # GLM 5, 198K context, tools, reasoning — multilingual, Russian OK
    "minimax-m3",          # MiniMax M3, 1M context, tools, reasoning, text+image — multilingual
    "perplexity-fast",     # Sonar, 128K context — fast web search
    "perplexity",          # Sonar Pro, 200K context — web search
    "perplexity-deep",     # Sonar deep search — deep search
    # "midijourney",       # REMOVED: text-to-music model, NOT for chat!
    "qwen-vision",         # Qwen Vision, 131K context, tools, text+image — vision specialist
    "kimi",                # Kimi K2.5, 262K context, tools, reasoning+vision
    "kimi-k2.6",           # Kimi K2.6, 262K context — latest reasoning
    "polly",               # Polly, reasoning model with tools — Pollinations reasoning
    # Models that may return empty content — kept for when they work
    # NOTE: "openai-fast" removed from CHAT_MODELS — returns empty too often
    "step-flash",          # Step Flash, 256K context, tools, reasoning+vision (may return empty)
    "qwen-large",          # Qwen Large, 1M context, reasoning+vision (may return empty)
    # Premium / quality models
    "mistral-large",       # Mistral Large, 256K context, tools — premium multilingual
    "gpt-5.5",             # GPT-5.5, 1M context — flagship
    "openai-large",        # GPT-5.4 reasoning, 400K context — best reasoning
    "deepseek-pro",        # DeepSeek V4 Pro, 1M context — best reasoning
    "grok-large",          # Grok 4 Large, 262K context — powerful reasoning
    "grok-4.3",            # Grok 4.3, 262K context — latest Grok
    "qwen-coder",          # Qwen Coder, 262K context, tools — structured content
    # Additional models
    "nova-micro",          # Amazon Nova Micro — ultra fast, cheapest
    "nova-2",              # Nova 2 Lite — fast, Russian OK
    "mistral-small",       # Mistral Small 3.2, 24B, tools, text+image — fast vision
    "mistral-small-3.2",   # Mistral Small 3.2 (alias), 24B — fastest vision
    "deepseek-v4",         # DeepSeek V4 Flash — fast alias
    "llama-3.3",           # Llama 3.3 70B (explicit), 131K context
    "llama-4-scout",       # Llama 4 Scout (alias), 328K context
    "step-3.5-flash",      # Step 3.5 Flash, 262K context, tools, reasoning — fast reasoning
    "openai-reasoning",    # OpenAI Reasoning — reasoning+vision
    "qwen3-coder",         # Qwen3 Coder 30B, tools — code reasoning
    "qwen-vision-pro",     # Qwen Vision Pro, 262K context, reasoning, text+image — pro vision
    "qwen-safety",         # Qwen Safety — content moderation
    "perplexity-reasoning",# Sonar Reasoning Pro — search+reasoning
    "minimax",             # MiniMax M2, 200K context, tools, reasoning — good chat
    # PAID-ONLY models (return 402 without paid balance) — kept for paid users
    "claude",              # Claude — premium, needs paid balance
    "gemini",              # Gemini — needs paid balance
    "gemini-large",        # Gemini Large — needs paid balance
    "llama-maverick",      # Llama Maverick — needs paid balance
    # Models needing special handling
    "openai-audio",        # OpenAI Audio — needs audio input, empty for text-only
]

# Reasoning models — for complex analysis and diagnostics (17 models)
REASONING_MODELS = [
    "openai-large",        # GPT-5.4 reasoning — best reasoning
    "gpt-5.5",             # GPT-5.5 — flagship
    "deepseek-pro",        # DeepSeek V4 Pro — best reasoning
    "deepseek",            # DeepSeek V4 — fast reasoning
    "grok-large",          # Grok Large — powerful reasoning
    "grok-4.3",            # Grok 4.3 — latest Grok reasoning
    "kimi",                # Kimi K2.5 — reasoning+vision
    "kimi-k2.6",           # Kimi K2.6 — latest reasoning
    "mistral-large",       # Mistral Large — premium reasoning
    "mistral-4",           # Mistral 4 — reasoning
    "gemma",               # Gemma 4 — MoE reasoning
    "minimax-m3",          # MiniMax M3 — reasoning
    "polly",               # Polly — Pollinations reasoning
    "qwen-large",          # Qwen Large — reasoning+vision (may return empty)
    "step-flash",          # Step Flash — reasoning+vision (may return empty)
    "openai-reasoning",    # OpenAI Reasoning — reasoning+vision
    "perplexity-reasoning",# Sonar Reasoning Pro — search+reasoning
]

# Vision models — can understand images (22 models)
VISION_MODELS = [
    "openai",              # Primary vision model
    "openai-large",        # Vision + reasoning
    "gpt-5.5",             # GPT-5.5 vision
    "mistral",             # Vision capable — fast
    "mistral-4",           # Vision + reasoning
    "mistral-small",       # Vision capable — fast
    "mistral-small-3.2",   # Vision capable — fastest
    "qwen-vision",         # Qwen Vision specialist
    "qwen-vision-pro",     # Qwen Vision Pro — pro vision
    "qwen-large",          # Qwen Large — reasoning+vision (may return empty)
    "llama-scout",         # Vision + long context
    "nova",                # Vision capable — good quality
    "nova-fast",           # Vision capable — ultra fast
    "minimax-m3",          # Vision capable — multilingual
    "gemma",               # Vision capable — MoE
    "grok",                # Vision capable — multilingual
    "grok-4.3",            # Vision capable — latest
    "step-flash",          # Vision capable (may return empty)
    "kimi-k2.6",           # Vision capable — latest reasoning
    "kimi",                # Vision capable — reasoning+vision
    "openai-reasoning",    # Reasoning + vision
    "polly",               # Vision capable — reasoning
]

# Content creation models — for generating channel posts (15 models)
CONTENT_MODELS = [
    "openai-large",        # Best quality for content
    "gpt-5.5",             # GPT-5.5 — best overall
    "deepseek",            # Good analysis
    "deepseek-pro",        # Strong reasoning for content
    "mistral-large",       # High-quality writing
    "mistral-4",           # Good writing
    "kimi",                # Reasoning+vision
    "grok-large",          # Good Russian writing
    "grok-4.3",            # Latest Grok
    "minimax-m3",          # Detailed content
    "qwen-large",          # Reasoning+vision (may return empty)
    "perplexity",          # Web search for content
    "perplexity-deep",     # Deep search for content
    "nova",                # Good quality
    "gemma",               # Good MoE content
    "polly",               # Slow but thorough
]

# Perplexity models — web-search augmented (4 models)
SEARCH_MODELS = [
    "perplexity",          # Sonar Pro
    "perplexity-fast",     # Sonar — fast
    "perplexity-deep",     # Sonar deep search
    "perplexity-reasoning",# Sonar Reasoning Pro, web + reasoning
]

# Image generation models (8 models)
IMAGE_MODELS = [
    "flux",                # Flux — text→image
    "gptimage",            # GPT Image — text→image
    "gptimage-large",      # GPT Image Large — text→image
    "kontext",             # Kontext — text+image→image
    "zimage",              # ZImage — text→image
    "nova-canvas",         # Nova Canvas — text+image→image
    "klein",               # Klein — text→image
    "ltx-2",               # LTX-2 — text→image
]

# Audio / transcription models (3 models)
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

# Fallback models — ordered by reliability and speed
FALLBACK_MODELS = [
    "mistral-4", "deepseek", "nova-fast", "grok", "grok-large",
    "grok-4.3", "minimax-m3", "llama-scout", "gemma", "kimi", "kimi-k2.6", "glm",
    "mistral", "step-flash", "polly", "mistral-large", "qwen-large",
    "gpt-5.4-mini", "deepseek-pro", "perplexity-deep",
    "perplexity-reasoning", "openai-reasoning", "nova-micro", "nova",
    "llama", "perplexity-fast", "perplexity", "qwen-coder",
    "qwen-vision", "nova-2", "mistral-small", "mistral-small-3.2",
]

# Models known to sometimes return empty content
EMPTY_CONTENT_MODELS = {
    "openai-fast", "step-flash", "qwen-large", "openai-audio",
}

# Paid-only models (return 402 without paid balance)
PAID_ONLY_MODELS = {
    "claude", "gemini", "gemini-large", "llama-maverick",
}

# Track model failures for circuit breaking
_model_failures: Dict[str, float] = {}
_FAILURE_COOLDOWN = 300  # 5 minutes


class PollinationsProvider(BaseAIProvider):
    """Pollinations AI provider v5.0 — DUAL KEY + FREE API FALLBACK + 60+ MODELS!

    Uses gen.pollinations.ai/v1/chat/completions (OpenAI-compatible).
    Falls back to text.pollinations.ai (FREE, no auth) when keys are depleted.

    FAILOVER CHAIN (within provider):
      1. Try KEY1 first (config.POLLINATIONS_API_KEY)
      2. On 402/401 from KEY1 -> switch to KEY2 (config.POLLINATIONS_API_KEY_2)
      3. On 402/401 from KEY2 -> try FREE API (text.pollinations.ai, no auth)
      4. Depleted keys auto-retry after 600 seconds cooldown
      5. Free API always available but rate-limited and slower

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
        # KEY1 = primary key from config
        self._api_key_1: str = config.POLLINATIONS_API_KEY
        self._api_key_2: str = config.POLLINATIONS_API_KEY_2
        # ── Per-key balance depletion tracking ──
        # 0 = active/never depleted; >0 = timestamp when depleted
        self._key1_depleted_at: float = 0.0
        self._key2_depleted_at: float = 0.0

        # ── Free API endpoints (no auth required) ──
        self._free_text_url: str = config.POLLINATIONS_FREE_TEXT_URL
        self._free_image_url: str = config.POLLINATIONS_FREE_IMAGE_URL
        self._free_api_available: bool = True  # Assume available until proven otherwise
        self._free_api_cooldown_until: float = 0.0  # Timestamp when free API cooldown ends

    # ── API Key Management ──────────────────────────────────────

    def _is_key_available(self, key_index: int) -> bool:
        """Check if an API key is available (not depleted or cooldown expired)."""
        if key_index == 1:
            depleted_at = self._key1_depleted_at
            key_val = self._api_key_1
        elif key_index == 2:
            depleted_at = self._key2_depleted_at
            key_val = self._api_key_2
        else:
            return False  # Only KEY1 and KEY2 supported

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
        """Determine which key/tier to use for the next request."""
        if self._is_key_available(1):
            return self._api_key_1, 1
        if self._is_key_available(2):
            return self._api_key_2, 2
        return "", 0

    def _get_key_status_summary(self) -> str:
        """Get a human-readable summary of key statuses."""
        parts = []
        for idx, (key_val, depleted_at) in enumerate([
            (self._api_key_1, self._key1_depleted_at),
            (self._api_key_2, self._key2_depleted_at),
        ], start=1):
            if not key_val:
                parts.append(f"KEY{idx}=not_set")
            elif depleted_at == 0:
                parts.append(f"KEY{idx}=active")
            else:
                remaining = KEY_COOLDOWN - (time.time() - depleted_at)
                parts.append(f"KEY{idx}=depleted({remaining:.0f}s)")
        return ", ".join(parts)

    def _build_key_tier_list(self) -> List[Tuple[str, int]]:
        """Build ordered list of available key tiers to try."""
        tiers: List[Tuple[str, int]] = []
        if self._is_key_available(1):
            tiers.append((self._api_key_1, 1))
        if self._is_key_available(2):
            tiers.append((self._api_key_2, 2))
        return tiers

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
        1. Try with KEY1 first (if balance available)
        2. On 402/401 from KEY1 -> switch to KEY2
        3. On 402/401 from KEY2 -> return error (router decides fallback)

        Empty content handling:
        - If a model returns empty content, check reasoning_content field
        - If both are empty, mark as temporary failure (circuit break)
        - Models in EMPTY_CONTENT_MODELS get special logging
        - Models are NEVER removed from lists
        """
        model = model or DEFAULT_MODEL

        # Check if model is in cooldown from recent failures
        if self._is_model_in_cooldown(model):
            alt = self._get_available_model()
            if alt:
                logger.info(f"Model {model} in cooldown, using {alt}")
                model = alt

        # Build key tier list: active keys first
        tiers_to_try = self._build_key_tier_list()

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

                async with httpx.AsyncClient(timeout=30.0) as client:
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
                            # Primary content
                            text = msg.get("content", "") or ""
                            # Fallback: reasoning_content (some reasoning models
                            # put their output here instead of content)
                            if not text:
                                text = msg.get("reasoning_content", "") or ""

                        usage = data.get("usage", {})
                        tokens_used = usage.get("total_tokens", 0)
                        actual_model = data.get("model", model)
                        tier_label = f"KEY{tier_index}"

                        # Handle empty content gracefully
                        if not text:
                            # Check if this is a known empty-content model
                            if model in EMPTY_CONTENT_MODELS:
                                logger.warning(
                                    f"Empty response from {model} (known empty-content model) "
                                    f"via {tier_label} — marking as temporary failure"
                                )
                            else:
                                logger.warning(
                                    f"Empty response from {model} via {tier_label} — "
                                    f"marking as temporary failure"
                                )
                            # Circuit break the model, but do NOT remove it
                            _model_failures[model] = time.time()
                            last_error = f"Empty response from {model}"
                            continue  # Try next key tier

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

        # ── All key tiers failed → return error, let router decide ──
        key_status = self._get_key_status_summary()
        logger.error(
            f"All API key tiers failed for model {model}. "
            f"Key status: [{key_status}]. Last error: {last_error}. "
            f"Router should decide fallback."
        )

        return AIResponse(
            text="",
            model=model,
            provider=self.name,
            error=True,
            error_message=f"All API keys depleted/unavailable [{key_status}]. {last_error}",
        )

    async def generate_image(self, prompt: str, model: str = "flux") -> Optional[bytes]:
        """Generate an image using Pollinations image API.
        Uses dual-key failover: KEY1 -> KEY2.
        Returns image bytes or None on failure.
        """
        # Build key tier list
        tiers_to_try = self._build_key_tier_list()

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

                async with httpx.AsyncClient(timeout=90.0) as client:
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
        """Analyze an image using vision-capable models.
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
        """Check if Pollinations API is reachable (with key or free)."""
        active_key, _ = self._get_active_key_tier()
        if active_key:
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    response = await client.get(
                        f"{self.base_url}/v1/models",
                        headers={"Authorization": f"Bearer {active_key}"},
                    )
                    return response.status_code == 200
            except Exception:
                pass
        # Also check free API
        return self._is_free_api_available()

    def _is_free_api_available(self) -> bool:
        """Check if free Pollinations API is available."""
        if not self._free_text_url:
            return False
        if self._free_api_cooldown_until > time.time():
            return False
        return self._free_api_available

    def _mark_free_api_cooldown(self, duration: float = 60.0) -> None:
        """Put free API on cooldown after failures."""
        self._free_api_cooldown_until = time.time() + duration
        logger.warning(f"Free Pollinations API on cooldown for {duration:.0f}s")

    # ── FREE API METHODS (no auth) ───────────────────────────────────

    async def chat_free(
        self,
        messages: List[Dict[str, str]],
        model: str = "openai",
        temperature: float = 0.7,
        max_tokens: int = 2048,
    ) -> AIResponse:
        """Send a chat completion request using FREE Pollinations API.

        Uses text.pollinations.ai WITHOUT Authorization header.
        This is the fallback when all API keys are depleted.
        Free API may be rate-limited and slower.
        """
        if not self._is_free_api_available():
            return AIResponse(
                text="",
                model=model,
                provider=f"{self.name}-free",
                error=True,
                error_message="Free API unavailable or on cooldown",
            )

        model = model or DEFAULT_MODEL

        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        try:
            headers = {"Content-Type": "application/json"}
            # NO Authorization header — this is the free API

            async with httpx.AsyncClient(timeout=25.0) as client:
                start_time = time.time()
                url = f"{self._free_text_url}/openai/chat/completions"
                response = await client.post(url, headers=headers, json=payload)
                elapsed = time.time() - start_time

                if response.status_code == 200:
                    data = response.json()
                    text = ""
                    choices = data.get("choices", [])
                    if choices:
                        msg = choices[0].get("message", {})
                        text = msg.get("content", "") or ""
                        if not text:
                            text = msg.get("reasoning_content", "") or ""

                    if not text:
                        logger.warning(f"Free API: empty response from {model}")
                        return AIResponse(
                            text="",
                            model=model,
                            provider=f"{self.name}-free",
                            error=True,
                            error_message="Empty response from free API",
                        )

                    logger.info(
                        f"Free API response: model={model}, "
                        f"time={elapsed:.1f}s, length={len(text)}"
                    )

                    return AIResponse(
                        text=text,
                        model=model,
                        provider=f"{self.name}-free",
                    )

                elif response.status_code == 429:
                    logger.warning(f"Free API rate limited (429)")
                    self._mark_free_api_cooldown(120.0)  # 2 min cooldown
                    return AIResponse(
                        text="",
                        model=model,
                        provider=f"{self.name}-free",
                        error=True,
                        error_message="Free API rate limited",
                    )

                else:
                    error_text = response.text[:300]
                    logger.error(f"Free API error: {response.status_code}: {error_text}")
                    self._mark_free_api_cooldown(60.0)
                    return AIResponse(
                        text="",
                        model=model,
                        provider=f"{self.name}-free",
                        error=True,
                        error_message=f"Free API HTTP {response.status_code}: {error_text}",
                    )

        except httpx.TimeoutException:
            logger.error(f"Free API timeout: model={model}")
            self._mark_free_api_cooldown(30.0)
            return AIResponse(
                text="",
                model=model,
                provider=f"{self.name}-free",
                error=True,
                error_message="Free API timeout",
            )

        except Exception as e:
            logger.error(f"Free API exception: {e}")
            self._mark_free_api_cooldown(60.0)
            return AIResponse(
                text="",
                model=model,
                provider=f"{self.name}-free",
                error=True,
                error_message=f"Free API exception: {e}",
            )

    async def analyze_image_free(
        self,
        image_url: str = "",
        image_base64: str = "",
        prompt: str = "Опиши подробно что ты видишь на этом изображении.",
        model: str = "openai",
        system_prompt: str = "",
        max_tokens: int = 600,
        temperature: float = 0.7,
    ) -> AIResponse:
        """Analyze an image using FREE Pollinations API (no auth).
        Same format as analyze_image but without API key.
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
            clean_base64 = image_base64
            if image_base64.startswith("data:"):
                header, clean_base64 = image_base64.split(",", 1)
                if "png" in header:
                    media_type = "image/png"
                elif "webp" in header:
                    media_type = "image/webp"
                elif "gif" in header:
                    media_type = "image/gif"

            content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:{media_type};base64,{clean_base64}"
                }
            })

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})

        messages.append({"role": "user", "content": content})

        # Try with a few vision models on free API
        for vision_model in [model, "mistral", "openai"]:
            if vision_model != model and self._is_model_in_cooldown(vision_model):
                continue
            result = await self.chat_free(
                messages=messages,
                model=vision_model,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            if not result.error and result.text:
                return result

        return AIResponse(
            text="",
            model=model,
            provider=f"{self.name}-free",
            error=True,
            error_message="Free API vision failed",
        )

    async def generate_image_free(self, prompt: str, model: str = "flux") -> Optional[bytes]:
        """Generate an image using FREE Pollinations API (no auth).

        Uses image.pollinations.ai/prompt/{encoded_prompt} — simple GET request.
        Returns image bytes or None on failure.
        """
        if not self._free_image_url:
            return None

        try:
            import urllib.parse
            encoded_prompt = urllib.parse.quote(prompt)
            url = f"{self._free_image_url}/prompt/{encoded_prompt}"
            # Add model and size params
            url += f"?model={model}&width=1344&height=768&nologo=true"

            async with httpx.AsyncClient(timeout=90.0) as client:
                response = await client.get(url)
                if response.status_code == 200:
                    content_type = response.headers.get("content-type", "")
                    if "image" in content_type or len(response.content) > 10000:
                        logger.info(f"Free image API: generated image ({len(response.content)} bytes)")
                        return response.content
                    else:
                        logger.warning(f"Free image API: unexpected content type: {content_type}")
                else:
                    logger.error(f"Free image API error: {response.status_code}")

        except Exception as e:
            logger.error(f"Free image API exception: {e}")

        return None

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
