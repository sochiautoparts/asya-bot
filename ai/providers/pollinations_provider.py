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
import asyncio
import base64
import logging
import random
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
    "openai-large",        # GPT-5.4 reasoning, 400K context — best reasoning
    "deepseek-pro",        # DeepSeek V4 Pro, 1M context — best reasoning
    "grok-large",          # Grok 4 Large, 262K context — powerful reasoning
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
    # NOTE: Paid-only and audio-only models moved to separate lists below.
    # They are NOT in CHAT_MODELS to avoid 402 errors and empty responses.
]

# Reasoning models — for complex analysis and diagnostics (17 models)
REASONING_MODELS = [
    "openai-large",        # GPT-5.4 reasoning — best reasoning
    "deepseek-pro",        # DeepSeek V4 Pro — best reasoning
    "deepseek",            # DeepSeek V4 — fast reasoning
    "grok-large",          # Grok Large — powerful reasoning
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
    "step-flash",          # Vision capable (may return empty)
    "kimi-k2.6",           # Vision capable — latest reasoning
    "kimi",                # Vision capable — reasoning+vision
    "openai-reasoning",    # Reasoning + vision
    "polly",               # Vision capable — reasoning
]

# Content creation models — for generating channel posts (15 models)
CONTENT_MODELS = [
    "openai-large",        # Best quality for content
    "deepseek",            # Good analysis
    "deepseek-pro",        # Strong reasoning for content
    "mistral-large",       # High-quality writing
    "mistral-4",           # Good writing
    "kimi",                # Reasoning+vision
    "grok-large",          # Good Russian writing
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

# Paid-only models (return 402 without paid balance) — separate from main lists
# These are only tried as a last resort; they will fail for free-tier users.
PAID_ONLY_MODELS = {
    "claude",              # Claude — premium, needs paid balance
    "gemini",              # Gemini — needs paid balance
    "gemini-large",        # Gemini Large — needs paid balance
    "llama-maverick",      # Llama Maverick — needs paid balance
}

# Fictional/unverified models — not confirmed in Pollinations API, removed from main lists
# Kept here for reference; do NOT add to CHAT_MODELS or other active lists
FICTIONAL_MODELS = {
    "gpt-5.4-mini",       # Not confirmed — may not exist
    "gpt-5.5",            # Not confirmed — may not exist
    "grok-4.3",           # Not confirmed — may not exist
    "openai-audio",       # Needs audio input, always empty for text
}

# Models known to sometimes return empty content
EMPTY_CONTENT_MODELS = {
    "openai-fast", "step-flash", "qwen-large", "openai-audio",
}

# ── Per-Model Circuit Breaker ──────────────────────────────────────────────

class CircuitBreaker:
    """Circuit breaker with closed → open → half-open state transitions.

    Tracks failures per model/endpoint and prevents requests when
    the failure threshold is exceeded (open state). After a recovery
    timeout, transitions to half-open to test if the service is back.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 300.0,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.failure_count: int = 0
        self.last_failure_time: float = 0.0
        self.state: str = "closed"  # closed | open | half-open

    @property
    def is_open(self) -> bool:
        """Check if the circuit breaker is open (should skip requests)."""
        if self.state == "open":
            if time.time() - self.last_failure_time > self.recovery_timeout:
                self.state = "half-open"
                return False  # Allow a test request
            return True  # Still in cooldown
        return False  # closed or half-open — allow request

    def record_success(self) -> None:
        """Record a successful request — reset to closed."""
        self.failure_count = 0
        self.state = "closed"

    def record_failure(self) -> None:
        """Record a failed request — may transition to open."""
        self.failure_count += 1
        self.last_failure_time = time.time()
        if self.failure_count >= self.failure_threshold:
            self.state = "open"
            logger.warning(
                "Circuit breaker OPEN after %d failures", self.failure_count
            )


# Per-model circuit breakers (replaces old _model_failures dict)
_model_circuits: Dict[str, CircuitBreaker] = {}
_FAILURE_COOLDOWN = 300  # 5 minutes (kept for compatibility)


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

        # ── Gen API failure tracking (Optimization #3) ──
        # When gen API has 3+ consecutive failures, invert priority and try free API first
        self._gen_fail_count: int = 0

        # ── Legacy/Free API circuit breaker (Optimization #8) ──
        # Separate circuit breaker for free API with lower threshold
        self._legacy_circuit = CircuitBreaker(failure_threshold=3, recovery_timeout=120.0)

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

    def _should_try_legacy_first(self) -> bool:
        """If gen API has been failing consistently, try legacy first.

        When the gen API has 3+ consecutive failures, we invert the normal
        priority and try the free/legacy API first. This avoids wasting time
        on a failing gen API when the free API might respond immediately.
        """
        return self._gen_fail_count >= 3

    def _get_model_circuit(self, model: str) -> CircuitBreaker:
        """Get or create a circuit breaker for a specific model."""
        if model not in _model_circuits:
            _model_circuits[model] = CircuitBreaker(
                failure_threshold=5,
                recovery_timeout=300.0,
            )
        return _model_circuits[model]

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

        # Check if model is in cooldown from recent failures (circuit breaker)
        circuit = self._get_model_circuit(model)
        if circuit.is_open:
            alt = self._get_available_model()
            if alt:
                logger.info(f"Model {model} circuit breaker OPEN, using {alt}")
                model = alt

        # Build key tier list: active keys first
        tiers_to_try = self._build_key_tier_list()

        # If no keys available at all, check cooldown
        if not tiers_to_try:
            # Gen API has no available keys — track failure for smart fallback
            self._gen_fail_count += 1
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

                # Use 10s timeout for chat requests — fast response is critical for user experience.
                # The overall chat timeout in _process_text_message is 45s, and we need to
                # try multiple key tiers and models before that deadline.
                # REDUCED from 15s to 10s — if Pollinations doesn't respond in 10s, 
                # Cloudflare (concurrent) will likely have responded already.
                async with httpx.AsyncClient(timeout=10.0) as client:
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
                            self._get_model_circuit(model).record_failure()
                            last_error = f"Empty response from {model}"
                            continue  # Try next key tier

                        logger.info(
                            f"Pollinations response ({tier_label}): model={actual_model}, "
                            f"tokens={tokens_used}, time={elapsed:.1f}s, "
                            f"length={len(text)}"
                        )

                        # Gen API success — reset failure counter
                        self._gen_fail_count = 0
                        self._get_model_circuit(model).record_success()

                        return AIResponse(
                            text=text,
                            model=actual_model,
                            provider=self.name,
                            tokens_used=tokens_used,
                        )

                    elif response.status_code in (401, 402):
                        # Balance depleted or unauthorized → switch key
                        # For paid-only models, don't deplete the key — it's the model that's paid, not the key
                        if model in PAID_ONLY_MODELS:
                            logger.warning(
                                f"HTTP {response.status_code} from paid-only model {model} via KEY{tier_index} — "
                                f"skipping key depletion for paid model"
                            )
                            self._get_model_circuit(model).record_failure()
                            last_error = f"HTTP {response.status_code} from paid-only model {model}"
                            continue  # Try next key tier (won't help, but consistent)
                        self._mark_key_depleted(tier_index)
                        self._gen_fail_count += 1
                        last_error = f"HTTP {response.status_code} from KEY{tier_index}"
                        logger.warning(
                            f"HTTP {response.status_code} from {model} via KEY{tier_index} — "
                            f"switching to next key tier"
                        )
                        continue  # Try next key tier

                    elif response.status_code == 429:
                        # Rate limited — short cooldown
                        logger.warning(f"Rate limited (429) on KEY{tier_index}, model={model}")
                        self._get_model_circuit(model).record_failure()
                        self._gen_fail_count += 1
                        # Try next key tier
                        continue

                    else:
                        error_text = response.text[:500]
                        logger.error(
                            f"Pollinations error: status={response.status_code}, "
                            f"model={model}, error={error_text}"
                        )
                        self._get_model_circuit(model).record_failure()
                        self._gen_fail_count += 1

                        return AIResponse(
                            text="",
                            model=model,
                            provider=self.name,
                            error=True,
                            error_message=f"HTTP {response.status_code}: {error_text}",
                        )

            except httpx.TimeoutException:
                logger.error(f"Pollinations timeout: model={model}, tier=KEY{tier_index}")
                self._get_model_circuit(model).record_failure()
                self._gen_fail_count += 1
                last_error = f"Timeout from KEY{tier_index}"
                # Mark the key as potentially depleted on timeout too —
                # if it times out, trying the same key again is unlikely to help
                if tier_index == 1 and self._key1_depleted_at == 0:
                    self._key1_depleted_at = time.time() - KEY_COOLDOWN + 60  # Retry in 60s instead of 600s
                elif tier_index == 2 and self._key2_depleted_at == 0:
                    self._key2_depleted_at = time.time() - KEY_COOLDOWN + 60
                continue  # Try next key tier

            except Exception as e:
                logger.error(f"Pollinations exception: model={model}, tier=KEY{tier_index}, error={e}")
                self._get_model_circuit(model).record_failure()
                self._gen_fail_count += 1
                last_error = str(e)
                continue

        # ── All key tiers failed → return error, let router decide ──
        key_status = self._get_key_status_summary()
        self._gen_fail_count += 1
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

    async def generate_image(self, prompt: str, model: str = "flux", **kwargs) -> AIResponse:
        """Generate an image using Pollinations image API.
        Uses dual-key failover: KEY1 -> KEY2.
        Returns AIResponse with image_b64 field on success.
        """
        start_time = time.time()

        # Build key tier list
        tiers_to_try = self._build_key_tier_list()

        if not tiers_to_try:
            logger.warning("No API keys available for image generation")
            return AIResponse(
                text="",
                model=model,
                provider=self.name,
                error="No API keys available for image generation",
            )

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
                                image_bytes = base64.b64decode(item["b64_json"])
                                return AIResponse(
                                    text="",
                                    model=model,
                                    provider=self.name,
                                    image_b64=base64.b64encode(image_bytes).decode('utf-8'),
                                    tokens_used=0,
                                    cached=False,
                                    latency_ms=int((time.time() - start_time) * 1000),
                                )
                            elif item.get("url"):
                                img_resp = await client.get(item["url"])
                                if img_resp.status_code == 200:
                                    return AIResponse(
                                        text="",
                                        model=model,
                                        provider=self.name,
                                        image_b64=base64.b64encode(img_resp.content).decode('utf-8'),
                                        tokens_used=0,
                                        cached=False,
                                        latency_ms=int((time.time() - start_time) * 1000),
                                    )
                    elif response.status_code in (401, 402):
                        self._mark_key_depleted(tier_index)
                        logger.warning(f"Image gen HTTP {response.status_code} via KEY{tier_index}, trying next tier...")
                        continue
                    else:
                        logger.error(f"Image generation error: {response.status_code} {response.text[:300]}")
                        return AIResponse(
                            text="",
                            model=model,
                            provider=self.name,
                            error=f"Image generation HTTP {response.status_code}",
                        )

            except Exception as e:
                logger.error(f"Image generation exception: {e}")
                continue

        return AIResponse(
            text="",
            model=model,
            provider=self.name,
            error="All API keys failed for image generation",
        )

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

        Protected by a separate legacy circuit breaker (threshold=3, recovery=120s).
        """
        # Check legacy circuit breaker first
        if self._legacy_circuit.is_open:
            logger.debug("Legacy API circuit breaker is OPEN, skipping")
            return AIResponse(
                text="",
                model=model,
                provider=f"{self.name}-free",
                error=True,
                error_message="Free API circuit breaker open",
            )

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

            # v5.2: 8s timeout (was 12s) — fail faster, free API is best-effort.
            # User is already waiting after the paid API attempt failed.
            async with httpx.AsyncClient(timeout=8.0) as client:
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
                        self._legacy_circuit.record_failure()
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

                    self._legacy_circuit.record_success()

                    return AIResponse(
                        text=text,
                        model=model,
                        provider=f"{self.name}-free",
                    )

                elif response.status_code == 429:
                    logger.warning(f"Free API rate limited (429)")
                    self._legacy_circuit.record_failure()
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
                    self._legacy_circuit.record_failure()
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
            self._legacy_circuit.record_failure()
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
            self._legacy_circuit.record_failure()
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

    async def generate_image_free(self, prompt: str, model: str = "flux", **kwargs) -> AIResponse:
        """Generate an image using FREE Pollinations API (no auth).

        Uses image.pollinations.ai/prompt/{encoded_prompt} — simple GET request.
        Returns AIResponse with image_b64 field on success.

        Implements retry with exponential backoff because the legacy API has
        a per-IP rate limit (1 concurrent request). Retries up to 5 times
        with increasing delays, changing seed on each retry to avoid cache.
        """
        start_time = time.time()

        # Check legacy circuit breaker
        if self._legacy_circuit.is_open:
            logger.debug("Legacy image API circuit breaker is OPEN, skipping")
            return AIResponse(
                text="",
                model=model,
                provider=f"{self.name}-free",
                error="Legacy image API circuit breaker open",
            )

        if not self._free_image_url:
            return AIResponse(
                text="",
                model=model,
                provider=f"{self.name}-free",
                error="Free image URL not configured",
            )

        import urllib.parse
        encoded_prompt = urllib.parse.quote(prompt)
        base_url = f"{self._free_image_url}/prompt/{encoded_prompt}"

        # Retry with exponential backoff
        max_retries = 5
        seed = kwargs.get("seed") or random.randint(1, 999999)

        for attempt in range(max_retries):
            params = {
                "model": model,
                "width": kwargs.get("width", 1344),
                "height": kwargs.get("height", 768),
                "nologo": "true",
                "seed": seed,
            }

            # Build URL with params
            param_str = "&".join(f"{k}={v}" for k, v in params.items())
            url = f"{base_url}?{param_str}"

            try:
                # NO Authorization header — free anonymous endpoint
                async with httpx.AsyncClient(timeout=90.0) as client:
                    response = await client.get(url)
                    elapsed = time.time() - start_time

                    if response.status_code == 200:
                        content_type = response.headers.get("content-type", "")
                        if ("image" in content_type or len(response.content) > 1000) and len(response.content) > 1000:
                            logger.info(f"Free image API: generated image ({len(response.content)} bytes, {elapsed:.1f}s)")
                            self._legacy_circuit.record_success()
                            direct_url = url
                            return AIResponse(
                                text="",
                                model=model,
                                provider=f"{self.name}-free",
                                image_b64=base64.b64encode(response.content).decode('utf-8'),
                                image_url=direct_url,
                                tokens_used=0,
                                cached=False,
                                latency_ms=int(elapsed * 1000),
                            )
                        elif len(response.content) <= 1000:
                            logger.warning(f"Free image API: image too small ({len(response.content)} bytes)")
                            # Try again with different seed
                            seed = random.randint(1, 999999)
                            if attempt < max_retries - 1:
                                wait = 10 * (attempt + 1)
                                logger.warning(f"Free image API: attempt {attempt+1}/{max_retries}, retrying in {wait}s")
                                await asyncio.sleep(wait)
                                continue
                        else:
                            logger.warning(f"Free image API: unexpected content type: {content_type}")
                            seed = random.randint(1, 999999)
                            if attempt < max_retries - 1:
                                wait = 10 * (attempt + 1)
                                await asyncio.sleep(wait)
                                continue

                    elif response.status_code in (402, 429):
                        # 402 = queue full for IP, 429 = rate limited
                        # Both are temporary — retry with backoff
                        if attempt < max_retries - 1:
                            wait = 10 * (attempt + 1)  # 10s, 20s, 30s, 40s, 50s
                            logger.warning(
                                f"Free image API rate limited (status {response.status_code}, "
                                f"attempt {attempt+1}/{max_retries}), waiting {wait}s"
                            )
                            seed = random.randint(1, 999999)  # Change seed to avoid cache
                            await asyncio.sleep(wait)
                            continue
                        else:
                            logger.warning(f"Free image API rate limited after {max_retries} retries")
                            self._legacy_circuit.record_failure()

                    else:
                        logger.error(f"Free image API error: {response.status_code}")
                        self._legacy_circuit.record_failure()
                        return AIResponse(
                            text="",
                            model=model,
                            provider=f"{self.name}-free",
                            error=f"Free image API HTTP {response.status_code}",
                        )

            except httpx.TimeoutException:
                logger.error(f"Free image API timeout (attempt {attempt+1}/{max_retries})")
                if attempt < max_retries - 1:
                    await asyncio.sleep(5)
                    continue
                self._legacy_circuit.record_failure()

            except Exception as e:
                logger.error(f"Free image API exception: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(2)
                    continue
                self._legacy_circuit.record_failure()

        return AIResponse(
            text="",
            model=model,
            provider=f"{self.name}-free",
            error="Free image generation failed after retries",
        )

    def _is_model_in_cooldown(self, model: str) -> bool:
        """Check if a model has recently failed and is in cooldown.

        Uses the per-model CircuitBreaker with closed→open→half-open states.
        When a circuit breaker is OPEN, the model is in cooldown.
        When it's HALF-OPEN, we allow one test request.

        IMPORTANT: We do NOT remove models from lists when they fail.
        Pollinations.ai rotates model availability — a failure today
        doesn't mean the model is permanently unavailable.
        """
        circuit = self._get_model_circuit(model)
        return circuit.is_open

    def _get_available_model(self) -> Optional[str]:
        """Get an available model that's not in cooldown (circuit not open)."""
        if not self._get_model_circuit(DEFAULT_MODEL).is_open:
            return DEFAULT_MODEL
        for model in FALLBACK_MODELS:
            if not self._get_model_circuit(model).is_open:
                return model
        # All models in cooldown — find the one closest to recovery
        if _model_circuits:
            # Return model with oldest last_failure_time (closest to recovery)
            oldest = min(_model_circuits, key=lambda m: _model_circuits[m].last_failure_time)
            return oldest
        return DEFAULT_MODEL

    def get_model_list(self) -> List[str]:
        """Return list of available models."""
        return POLLINATIONS_MODELS.copy()
