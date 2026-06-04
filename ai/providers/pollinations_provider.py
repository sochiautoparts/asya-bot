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
CHAT_MODELS = [
    "openai",              # GPT-5.4, 400K context, tools, text+image
    "openai-fast",         # GPT-5 nano, 400K context, tools — fastest OpenAI
    "openai-large",        # GPT-5.4 reasoning, 400K context, tools
    "gpt-5.4-mini",        # GPT-5.4 mini, 400K context, tools
    "gpt-5.5",             # GPT-5.5, 1M context, tools, reasoning
    "mistral",             # Mistral Small, 128K context, tools, text+image
    "mistral-large",       # Mistral Large, 256K context, tools, reasoning
    "mistral-4",           # Mistral 4, 262K context, tools, reasoning
    "mistral-small",       # Mistral Small (fast), 128K, tools, text+image — NEW
    "deepseek",            # DeepSeek V4, 1M context, tools, reasoning
    "deepseek-pro",        # DeepSeek V4 Pro, 1M context, tools, reasoning
    "qwen-coder",          # Qwen Coder, 262K context, tools
    "llama",               # Llama 3.3 70B, 131K context, tools
    "llama-scout",         # Llama 4 Scout, 328K context, tools, text+image
    "nova",                # Nova 2, 1M context, tools, reasoning, text+image
    "nova-fast",           # Nova Micro, 128K context, tools — very fast
    "grok",                # Grok 4, 262K context, tools, text+image
    "perplexity",          # Sonar Pro, 200K context — web search
    "perplexity-fast",     # Sonar, 128K context — fast web search
    "gemma",               # Gemma 4, 262K context, tools, reasoning, text+image
    "glm",                 # GLM 5, 198K context, tools, reasoning — Russian OK
    "minimax",             # MiniMax M2, 200K context, tools, reasoning
    "minimax-m3",          # MiniMax M3, 1M context, tools, reasoning, text+image
    "kimi",                # Kimi K2.5, 262K context, tools, reasoning
    "kimi-k2.6",           # Kimi K2.6, 262K context, tools, reasoning
    "step-3.5-flash",      # Step 3.5 Flash, 262K context, tools, reasoning
    "openai-mini",         # GPT Mini, 400K context, tools — very fast, great for quick responses
    "step-flash",          # Step Flash, 256K context, tools, reasoning, text+image
    "polly",               # Polly, reasoning model with tools
    "grok-large",          # Grok Large, 262K context, tools, reasoning, text+image
    "grok-4.3",            # Grok 4.3, 262K context, tools, reasoning, text+image
]

# Reasoning models — for complex analysis and diagnostics
REASONING_MODELS = [
    "openai-reasoning",    # GPT reasoning — complex reasoning + vision — NEW
    "openai-large",        # GPT-5.4 reasoning
    "qwen-large",          # Qwen Large, reasoning + vision — NEW
    "deepseek",            # DeepSeek V4, reasoning
    "deepseek-pro",        # DeepSeek V4 Pro, reasoning
    "mistral-4",           # Mistral 4, reasoning
    "step-flash",          # Step Flash, reasoning + image
    "polly",               # Polly, reasoning
    "grok-large",          # Grok Large, reasoning
    "grok-4.3",            # Grok 4.3, reasoning
]

# Vision models — can understand images
VISION_MODELS = [
    "openai",              # Primary vision model
    "openai-fast",         # Fast vision
    "openai-reasoning",    # Reasoning + vision — NEW
    "mistral",             # Vision capable
    "mistral-small",       # Vision capable — NEW
    "qwen-vision",         # Qwen Vision, 131K context, tools, text+image
    "qwen-vision-pro",     # Qwen Vision Pro, 262K context, reasoning, text+image
    "qwen-large",          # Qwen Large, reasoning + vision — NEW
    "llama-scout",         # Vision capable
    "nova",                # Vision capable
    "minimax-m3",          # Vision capable
    "gemma",               # Vision capable
    "grok",                # Vision capable
    "openai-mini",         # Vision capable
    "step-flash",          # Vision capable
]

# Content creation models — for generating channel posts
CONTENT_MODELS = [
    "openai-large",        # Best quality for content
    "openai-reasoning",    # Complex content with reasoning — NEW
    "qwen-large",          # Good for detailed content — NEW
    "deepseek",            # Good analysis
    "mistral-4",           # Good writing
    "step-flash",          # Good for content
    "polly",               # Slow but thorough
]

# Perplexity models — web-search augmented
SEARCH_MODELS = [
    "perplexity",          # Sonar Pro
    "perplexity-fast",     # Sonar
    "perplexity-deep",     # Sonar Deep
    "perplexity-reasoning",# Sonar Reasoning
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
FALLBACK_MODELS = ["openai-mini", "mistral-4", "deepseek", "nova-fast", "grok", "minimax", "llama-scout", "gemma", "kimi", "glm", "mistral-small", "step-flash"]

# Track model failures for circuit breaking
_model_failures: Dict[str, float] = {}
_FAILURE_COOLDOWN = 300  # 5 minutes


class PollinationsProvider(BaseAIProvider):
    """Pollinations AI provider — OpenAI-compatible API with multi-model support."""

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
            async with httpx.AsyncClient(timeout=90.0) as client:
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
                        # For reasoning models that use reasoning_content
                        if not text:
                            text = msg.get("reasoning_content", "")

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
