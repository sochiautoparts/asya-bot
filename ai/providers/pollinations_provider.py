"""
Pollinations AI Provider — OpenAI-compatible API at gen.pollinations.ai
Supports 30+ models including OpenAI, Mistral, DeepSeek, etc.
"""

import httpx
import json
import logging
import time
from typing import Optional, List, Dict

from ai.providers.base import BaseAIProvider, AIResponse
from bot.config import config

logger = logging.getLogger("asya.ai.pollinations")

# ── Available models (tested and verified working) ─────────────────────────────

POLLINATIONS_MODELS = [
    # ── OpenAI family ──────────────────────────────────────────────────────
    "openai",              # GPT-5.4, 400K context, tools, text+image
    "openai-fast",         # GPT-5 nano, 400K context, tools — fastest OpenAI
    "openai-large",        # GPT-5.4 reasoning, 400K context, tools
    "gpt-5.4-mini",        # GPT-5.4 mini, 400K context, tools
    "gpt-5.5",             # GPT-5.5, 1M context, tools, reasoning
    # ── Mistral family ─────────────────────────────────────────────────────
    "mistral",             # Mistral Small, 128K context, tools, text+image
    "mistral-large",       # Mistral Large, 256K context, tools, reasoning
    "mistral-4",           # Mistral 4, 262K context, tools, reasoning
    # ── DeepSeek family ───────────────────────────────────────────────────
    "deepseek",            # DeepSeek V4, 1M context, tools, reasoning
    "deepseek-pro",        # DeepSeek V4 Pro, 1M context, tools, reasoning
    # ── Qwen family ───────────────────────────────────────────────────────
    "qwen-coder",          # Qwen Coder, 262K context, tools
    "qwen-vision",         # Qwen Vision, 131K context, tools, text+image
    "qwen-vision-pro",     # Qwen Vision Pro, 262K context, reasoning, text+image
    # ── Llama family ──────────────────────────────────────────────────────
    "llama",               # Llama 3.3 70B, 131K context, tools
    "llama-scout",         # Llama 4 Scout, 328K context, tools, text+image
    # ── Amazon Nova family ────────────────────────────────────────────────
    "nova",                # Nova 2, 1M context, tools, reasoning, text+image
    "nova-fast",           # Nova Micro, 128K context, tools — very fast
    # ── Grok family ───────────────────────────────────────────────────────
    "grok",                # Grok 4, 262K context, tools, text+image
    # ── Perplexity (search-augmented) ──────────────────────────────────────
    "perplexity",          # Sonar Pro, 200K context — web search
    "perplexity-fast",     # Sonar, 128K context — fast web search
    "perplexity-deep",     # Sonar Deep, 128K context — deep web search
    "perplexity-reasoning",# Sonar Reasoning, 128K context
    # ── Other reasoning models ─────────────────────────────────────────────
    "gemma",               # Gemma 4, 262K context, tools, reasoning, text+image
    "glm",                 # GLM 5, 198K context, tools, reasoning — Russian OK
    "minimax",             # MiniMax M2, 200K context, tools, reasoning
    "minimax-m3",          # MiniMax M3, 1M context, tools, reasoning, text+image
    "kimi",                # Kimi K2.5, 262K context, tools, reasoning
    "kimi-k2.6",           # Kimi K2.6, 262K context, tools, reasoning
    "step-3.5-flash",      # Step 3.5 Flash, 262K context, tools, reasoning
    # ── Image generation models ─────────────────────────────────────────────
    "flux",                # Flux — text→image
    "gptimage",            # GPT Image — text→image
    "gptimage-large",      # GPT Image Large — text→image
    "kontext",             # Kontext — text+image→image
    "zimage",              # ZImage — text→image
    "nova-canvas",         # Nova Canvas — text+image→image
    # ── Audio / transcription ──────────────────────────────────────────────
    "whisper",             # Whisper — audio→text
    "universal-2",         # Universal 2 — audio→text
    "universal-3-pro",     # Universal 3 Pro — audio→text
]

DEFAULT_MODEL = "openai"
FALLBACK_MODELS = ["mistral-4", "deepseek", "nova-fast", "grok", "minimax", "llama-scout", "gemma", "kimi", "glm"]

# Track model failures for circuit breaking
_model_failures: Dict[str, float] = {}
_FAILURE_COOLDOWN = 300  # 5 minutes


class PollinationsProvider(BaseAIProvider):
    """Pollinations AI provider — OpenAI-compatible API."""

    def __init__(self):
        super().__init__(
            name="pollinations",
            api_key=config.POLLINATIONS_API_KEY,
            base_url=config.POLLINATIONS_BASE_URL,
        )

    async def chat(
        self,
        messages: List[Dict[str, str]],
        model: str = "",
        temperature: float = 0.7,
        max_tokens: int = 2048,
        **kwargs,
    ) -> AIResponse:
        """Send a chat completion request to Pollinations API."""
        model = model or DEFAULT_MODEL

        # Check if model is in cooldown from recent failures
        if self._is_model_in_cooldown(model):
            # Try a fallback model
            alt = self._get_available_model()
            if alt:
                logger.info(f"Model {model} in cooldown, using {alt}")
                model = alt

        url = f"{self.base_url}/v1/chat/completions"

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        # Add optional parameters
        if kwargs.get("top_p"):
            payload["top_p"] = kwargs["top_p"]
        if kwargs.get("frequency_penalty"):
            payload["frequency_penalty"] = kwargs["frequency_penalty"]
        if kwargs.get("presence_penalty"):
            payload["presence_penalty"] = kwargs["presence_penalty"]

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                start_time = time.time()
                response = await client.post(url, headers=headers, json=payload)
                elapsed = time.time() - start_time

                if response.status_code == 200:
                    data = response.json()
                    text = ""
                    choices = data.get("choices", [])
                    if choices:
                        text = choices[0].get("message", {}).get("content", "")

                    usage = data.get("usage", {})
                    tokens_used = usage.get("total_tokens", 0)
                    actual_model = data.get("model", model)

                    logger.info(
                        f"Pollinations response: model={actual_model}, "
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
                    error_text = response.text[:500]
                    logger.error(
                        f"Pollinations error: status={response.status_code}, "
                        f"model={model}, error={error_text}"
                    )
                    # Mark model as failed
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
            # Pollinations image generation endpoint
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
                    # Image is returned as base64
                    import base64
                    for item in data.get("data", []):
                        if item.get("b64_json"):
                            return base64.b64decode(item["b64_json"])
                        elif item.get("url"):
                            # Download from URL
                            img_resp = await client.get(item["url"])
                            if img_resp.status_code == 200:
                                return img_resp.content
                else:
                    logger.error(f"Image generation error: {response.status_code} {response.text[:300]}")

        except Exception as e:
            logger.error(f"Image generation exception: {e}")

        return None

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
        # First try the default
        if not self._is_model_in_cooldown(DEFAULT_MODEL):
            return DEFAULT_MODEL
        # Then try fallbacks
        for model in FALLBACK_MODELS:
            if not self._is_model_in_cooldown(model):
                return model
        # If all in cooldown, try the one with oldest failure
        if _model_failures:
            oldest = min(_model_failures, key=_model_failures.get)
            return oldest
        return DEFAULT_MODEL

    def get_model_list(self) -> List[str]:
        """Return list of available models."""
        return POLLINATIONS_MODELS.copy()
