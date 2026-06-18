"""Cloudflare Workers AI Provider — Multi-model with dual-account rotation

Multi-account Cloudflare Workers AI provider with:
  - TWO accounts with automatic rotation (each 10K req/day)
  - MULTI-MODEL TEXT FALLBACK: mistral-small → llama-3.3-70b → deepseek-r1
  - IMAGE GENERATION: Stable Diffusion XL via native /ai/run/ endpoint
  - OpenAI-compatible vision format (image_url in content array)
  - REST API via /client/v4/accounts/{id}/ai/run/ endpoint
  - Per-account daily request tracking and rotation
  - Circuit breaking for failed accounts

FAILOVER CHAIN (within provider):
  TEXT: For each account, try models in order: mistral-small → llama-3.3-70b → deepseek-r1
        Account 1 (all models) → Account 2 (all models) → Error
  IMAGE: For each account, try SDXL models with multi-size strategy
         Account 1 → Account 2 → Error

VISION FORMAT:
  Standard OpenAI-compatible content array with image_url type.
  Must use native /ai/run/ endpoint (NOT /v1/chat/completions).
"""

import asyncio
import base64
import httpx
import json
import logging
import time
from typing import Optional, List, Dict
from dataclasses import dataclass

from ai.providers.base import BaseAIProvider, AIResponse
from bot.config import config

logger = logging.getLogger("asya.ai.cloudflare")

# ── Cloudflare Workers AI Models ──
CF_MODEL = "@cf/mistralai/mistral-small-3.1-24b-instruct"

# Fallback text models — tried in order when primary model fails
CF_TEXT_MODELS = [
    "@cf/mistralai/mistral-small-3.1-24b-instruct",
    "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
    "@cf/deepseek-ai/deepseek-r1-distill-qwen-32b",
]

# Image generation models (Stable Diffusion XL on Cloudflare)
CF_IMAGE_MODELS = [
    "@cf/stabilityai/stable-diffusion-xl-base-1.0",
    "@cf/bytedance/stable-diffusion-xl-lightning",
]

# ── Per-account daily request limit ──
DAILY_REQUEST_LIMIT = 10000  # 10K per account per day

# ── Cooldown for failed accounts (seconds) ──
ACCOUNT_COOLDOWN = 300  # 5 minutes


@dataclass
class CFAccount:
    """Cloudflare account with request tracking."""
    account_id: str
    api_token: str
    index: int  # 1 or 2
    request_count: int = 0
    last_reset: float = 0.0  # Timestamp of last daily reset
    depleted_at: float = 0.0  # Timestamp when daily limit hit
    last_error: str = ""
    consecutive_empty_responses: int = 0  # Track empty responses before marking depleted

    def reset_if_new_day(self) -> None:
        """Reset daily request count if it's a new day."""
        now = time.time()
        if now - self.last_reset > 86400:  # 24 hours
            self.request_count = 0
            self.last_reset = now
            self.depleted_at = 0.0
            logger.info(f"CF Account {self.index}: daily counter reset ({self.account_id[:8]}...)")

    def is_available(self) -> bool:
        """Check if account is available for requests."""
        self.reset_if_new_day()

        if not self.account_id or not self.api_token:
            return False

        if self.depleted_at > 0:
            elapsed = time.time() - self.depleted_at
            if elapsed >= ACCOUNT_COOLDOWN:
                self.depleted_at = 0.0
                logger.info(f"CF Account {self.index}: cooldown expired, retrying")
                # Fall through to request_count check — don't return True blindly
            else:
                return False

        return self.request_count < DAILY_REQUEST_LIMIT

    def increment(self) -> None:
        """Increment request counter."""
        self.request_count += 1
        if self.request_count >= DAILY_REQUEST_LIMIT:
            self.depleted_at = time.time()
            logger.warning(
                f"CF Account {self.index}: daily limit reached "
                f"({self.request_count}/{DAILY_REQUEST_LIMIT})"
            )

    def mark_depleted(self, reason: str = "") -> None:
        """Mark account as depleted."""
        self.depleted_at = time.time()
        self.last_error = reason
        logger.warning(
            f"CF Account {self.index}: marked depleted. Reason: {reason}"
        )


class CloudflareProvider(BaseAIProvider):
    """Cloudflare Workers AI provider with dual-account rotation.

    Uses @cf/mistralai/mistral-small-3.1-24b-instruct model.
    Supports: chat, vision (image_url format), text generation.

    API endpoint: POST https://api.cloudflare.com/client/v4/accounts/{id}/ai/run/{model}
    Auth: Bearer token in Authorization header.
    """

    def __init__(self):
        super().__init__(
            name="cloudflare",
            api_key="",  # Managed per-account
            base_url="https://api.cloudflare.com/client/v4",
        )

        # Initialize two accounts from config
        self._accounts: List[CFAccount] = []
        self._current_account_idx: int = 0

        # Account 1
        if config.CF_ACCOUNT_ID_1 and config.CF_API_TOKEN_1:
            self._accounts.append(CFAccount(
                account_id=config.CF_ACCOUNT_ID_1,
                api_token=config.CF_API_TOKEN_1,
                index=1,
                last_reset=time.time(),
            ))

        # Account 2
        if config.CF_ACCOUNT_ID_2 and config.CF_API_TOKEN_2:
            self._accounts.append(CFAccount(
                account_id=config.CF_ACCOUNT_ID_2,
                api_token=config.CF_API_TOKEN_2,
                index=2,
                last_reset=time.time(),
            ))

        total_capacity = len(self._accounts) * DAILY_REQUEST_LIMIT
        logger.info(
            f"CloudflareProvider initialized: {len(self._accounts)} accounts, "
            f"model={CF_MODEL}, total_daily_capacity={total_capacity} requests"
        )

    def _get_active_account(self) -> Optional[CFAccount]:
        """Get the current active account, rotating if needed."""
        if not self._accounts:
            return None

        # Try current account first
        for _ in range(len(self._accounts)):
            account = self._accounts[self._current_account_idx]
            if account.is_available():
                return account
            # Rotate to next account
            self._current_account_idx = (self._current_account_idx + 1) % len(self._accounts)

        # All accounts depleted
        return None

    def _rotate_account(self) -> None:
        """Rotate to the next account."""
        if self._accounts:
            self._current_account_idx = (self._current_account_idx + 1) % len(self._accounts)

    def _build_url(self, account: CFAccount, model: str = "") -> str:
        """Build the API URL for a given account and model."""
        chosen_model = model or CF_MODEL
        return (
            f"{self.base_url}/accounts/{account.account_id}/ai/run/{chosen_model}"
        )

    def _build_headers(self, account: CFAccount) -> Dict[str, str]:
        """Build request headers for a given account."""
        return {
            "Authorization": f"Bearer {account.api_token}",
            "Content-Type": "application/json",
        }

    async def chat(
        self,
        messages: List[Dict[str, str]],
        model: str = "",
        temperature: float = 0.7,
        max_tokens: int = 2048,
        **kwargs,
    ) -> AIResponse:
        """Send a chat completion request to Cloudflare Workers AI.

        Multi-model failover: For each account, tries models in order:
          mistral-small-3.1 → llama-3.3-70b → deepseek-r1
        Then rotates to next account and repeats.

        This gives up to 6 attempts (2 accounts × 3 models) before giving up.
        """
        account = self._get_active_account()
        if not account:
            return AIResponse(
                text="",
                model=CF_MODEL,
                provider=self.name,
                error=True,
                error_message="All Cloudflare accounts depleted or unavailable",
            )

        # Try each available account
        tried_accounts = set()
        while account and account.index not in tried_accounts:
            tried_accounts.add(account.index)

            # Try each text model with this account before rotating
            for cf_model in CF_TEXT_MODELS:
                try:
                    url = self._build_url(account, cf_model)
                    headers = self._build_headers(account)

                    payload = {
                        "messages": messages,
                        "max_tokens": max_tokens,
                        "temperature": temperature,
                    }

                    async with httpx.AsyncClient(timeout=7.0) as client:
                        start_time = time.time()
                        response = await client.post(url, headers=headers, json=payload)
                        elapsed = time.time() - start_time

                        if response.status_code == 200:
                            data = response.json()
                            text = self._extract_text(data)

                            if not text:
                                logger.warning(
                                    f"CF Account {account.index}, model={cf_model}: "
                                    f"empty response, elapsed={elapsed:.1f}s — trying next model"
                                )
                                # Try next model with same account before rotating
                                continue

                            account.increment()
                            account.consecutive_empty_responses = 0
                            logger.info(
                                f"CF response (Account {account.index}): "
                                f"model={cf_model}, time={elapsed:.1f}s, "
                                f"length={len(text)}, requests_today={account.request_count}/{DAILY_REQUEST_LIMIT}"
                            )

                            return AIResponse(
                                text=text,
                                model=cf_model,
                                provider=self.name,
                            )

                        elif response.status_code in (401, 403):
                            error_text = response.text[:300]
                            logger.error(
                                f"CF Account {account.index}: auth error {response.status_code}: {error_text}"
                            )
                            account.mark_depleted(f"HTTP {response.status_code}")
                            # Auth error = account issue, not model issue — rotate account
                            break  # Break model loop, rotate to next account

                        elif response.status_code == 429:
                            logger.warning(f"CF Account {account.index}: rate limited (429)")
                            account.mark_depleted("Rate limited")
                            break  # Rate limit = account issue, not model issue — rotate account

                        elif response.status_code == 500:
                            error_text = response.text[:300]
                            logger.error(
                                f"CF Account {account.index}, model={cf_model}: "
                                f"server error 500: {error_text} — trying next model"
                            )
                            # 500 might be model-specific — try next model
                            continue

                        else:
                            error_text = response.text[:300]
                            logger.error(
                                f"CF Account {account.index}, model={cf_model}: "
                                f"HTTP {response.status_code}: {error_text} — trying next model"
                            )
                            # Try next model before giving up
                            continue

                except httpx.TimeoutException:
                    logger.error(f"CF Account {account.index}, model={cf_model}: request timeout — trying next model")
                    # Try next model
                    continue

                except Exception as e:
                    logger.error(f"CF Account {account.index}, model={cf_model}: exception: {e} — trying next model")
                    continue

            # All models failed for this account — rotate to next
            self._rotate_account()
            account = self._get_active_account()

        # All accounts failed
        return AIResponse(
            text="",
            model=CF_MODEL,
            provider=self.name,
            error=True,
            error_message="All Cloudflare accounts and models failed",
        )

    def _extract_text(self, data: dict) -> str:
        """Extract text from Cloudflare API response.

        Cloudflare Workers AI can return responses in multiple formats:
        1. OpenAI-compatible: { "choices": [{ "message": { "content": "..." } }] }
        2. Native CF format: { "result": { "response": "..." } }
        3. Native CF with choices: { "result": { "choices": [...] } }
        """
        # Try OpenAI-compatible format first
        choices = data.get("choices", [])
        if choices:
            msg = choices[0].get("message", {})
            text = msg.get("content", "") or ""
            if text:
                return text
            # Some reasoning models put output in reasoning_content
            text = msg.get("reasoning_content", "") or ""
            if text:
                return text

        # Try native Cloudflare format
        result = data.get("result", {})
        if result:
            # result.response (simple text)
            if isinstance(result, str):
                return result
            text = result.get("response", "")
            if text:
                return text
            # result with choices
            result_choices = result.get("choices", [])
            if result_choices:
                msg = result_choices[0].get("message", {})
                text = msg.get("content", "") or ""
                if text:
                    return text

        # Fallback: try to get any text from the response
        if isinstance(data, dict):
            for key in ["response", "content", "text", "output"]:
                val = data.get(key, "")
                if isinstance(val, str) and val:
                    return val

        return ""

    async def analyze_image(
        self,
        image_url: str = "",
        image_base64: str = "",
        prompt: str = "Опиши подробно что ты видишь на этом изображении.",
        system_prompt: str = "",
        max_tokens: int = 600,
        temperature: float = 0.7,
    ) -> AIResponse:
        """Analyze an image using Cloudflare Workers AI vision.

        Uses standard OpenAI-compatible content array with image_url type.
        This is the correct format for Mistral Small 3.1 on CF Workers AI.
        """
        # Build the content array for vision — OpenAI-compatible format
        content = [
            {"type": "text", "text": prompt}
        ]

        if image_url:
            content.append({
                "type": "image_url",
                "image_url": {"url": image_url}
            })
        elif image_base64:
            # Handle base64 image data
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

        return await self.chat(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    async def generate_image(
        self,
        prompt: str,
        width: int = 1024,
        height: int = 1024,
        model: str = "",
        **kwargs,
    ) -> AIResponse:
        """Generate an image using Cloudflare Workers AI (Stable Diffusion XL).

        Uses the native Workers AI binding endpoint for image generation.
        Multi-account failover — tries each account with each image model.
        Multi-size strategy: try requested size first, then smaller fallback.
        """
        chosen_model = model or CF_IMAGE_MODELS[0]
        if not chosen_model.startswith("@cf/"):
            chosen_model = CF_IMAGE_MODELS[0]

        # SDXL only supports certain dimensions — snap to nearest supported
        # Supported: 256x256, 512x512, 768x768, 896x1152, 1152x896, 1024x1024
        if width == height:
            if width <= 256:
                w, h = 256, 256
            elif width <= 512:
                w, h = 512, 512
            elif width <= 768:
                w, h = 768, 768
            else:
                w, h = 1024, 1024
        elif width > height:
            w, h = 1152, 896  # landscape
        else:
            w, h = 896, 1152  # portrait

        # Multi-size strategy: fast first, then quality, then smaller fallback
        size_configs = [
            (w, h, 8, 7.5),       # Fast: fewer steps
            (512, 512, 8, 7.5),    # Smaller size as fallback
            (w, h, 20, 7.5),       # Full quality
        ]

        for size_w, size_h, steps, guidance in size_configs:
            for img_model in CF_IMAGE_MODELS:
                for _ in range(len(self._accounts)):
                    account = self._get_active_account()
                    if not account:
                        break

                    start_time = time.time()
                    url = self._build_url(account, img_model)

                    payload = {
                        "prompt": prompt,
                        "width": size_w,
                        "height": size_h,
                        "num_steps": steps,
                        "guidance": guidance,
                    }

                    headers = self._build_headers(account)

                    try:
                        async with httpx.AsyncClient(timeout=90.0) as client:
                            response = await client.post(url, headers=headers, json=payload)
                            elapsed = time.time() - start_time

                            if response.status_code == 200:
                                content_type = response.headers.get("content-type", "")

                                if "image" in content_type:
                                    # CF returns image as binary (image/png)
                                    img_bytes = response.content
                                    if len(img_bytes) > 1000:
                                        img_b64 = base64.b64encode(img_bytes).decode("utf-8")
                                        account.increment()
                                        logger.info(
                                            f"CF image gen success: model={img_model}, "
                                            f"{len(img_bytes)} bytes, {elapsed:.1f}s"
                                        )
                                        return AIResponse(
                                            text="",
                                            model=img_model,
                                            provider=self.name,
                                            image_b64=img_b64,
                                        )
                                    else:
                                        logger.warning(f"CF image too small ({len(img_bytes)} bytes)")
                                        self._rotate_account()
                                        continue
                                else:
                                    # Might be JSON response with base64
                                    try:
                                        data = response.json()
                                        image_data = data.get("image", "")
                                        if not image_data and "result" in data:
                                            result = data["result"]
                                            if isinstance(result, dict):
                                                image_data = result.get("image", "")
                                            elif isinstance(result, str):
                                                image_data = result

                                        if image_data:
                                            try:
                                                decoded = base64.b64decode(image_data)
                                                if len(decoded) > 1000:
                                                    account.increment()
                                                    logger.info(
                                                        f"CF image gen success (JSON): model={img_model}, "
                                                        f"{len(decoded)} bytes, {elapsed:.1f}s"
                                                    )
                                                    return AIResponse(
                                                        text="",
                                                        model=img_model,
                                                        provider=self.name,
                                                        image_b64=image_data,
                                                    )
                                            except Exception:
                                                pass
                                    except Exception:
                                        pass

                                    self._rotate_account()
                                    continue

                            elif response.status_code == 429:
                                logger.warning(f"CF image gen rate limited on account {account.index}")
                                account.mark_depleted("Image gen rate limited")
                                self._rotate_account()
                                continue

                            elif response.status_code in (401, 403):
                                error_text = response.text[:200]
                                logger.error(f"CF image auth error on account {account.index}: {error_text}")
                                account.mark_depleted(f"HTTP {response.status_code}")
                                self._rotate_account()
                                continue

                            else:
                                error_text = response.text[:200]
                                logger.error(f"CF image gen error {response.status_code}: {error_text}")
                                self._rotate_account()
                                continue

                    except httpx.TimeoutException:
                        logger.error(f"CF image gen timeout on account {account.index}")
                        self._rotate_account()
                        continue

                    except Exception as e:
                        logger.error(f"CF image gen exception on account {account.index}: {e}")
                        self._rotate_account()
                        continue

        return AIResponse(
            text="",
            model=chosen_model,
            provider=self.name,
            error=True,
            error_message="Cloudflare image generation failed on all accounts/models",
        )

    async def is_available(self) -> bool:
        """Check if any Cloudflare account is available."""
        account = self._get_active_account()
        return account is not None

    def get_status(self) -> str:
        """Get a status summary of all accounts."""
        if not self._accounts:
            return "No Cloudflare accounts configured"

        parts = []
        for acc in self._accounts:
            acc.reset_if_new_day()
            if acc.depleted_at > 0:
                remaining = ACCOUNT_COOLDOWN - (time.time() - acc.depleted_at)
                parts.append(
                    f"Acc{acc.index}: depleted({remaining:.0f}s left), "
                    f"reqs={acc.request_count}/{DAILY_REQUEST_LIMIT}"
                )
            else:
                parts.append(
                    f"Acc{acc.index}: active, "
                    f"reqs={acc.request_count}/{DAILY_REQUEST_LIMIT}"
                )
        return " | ".join(parts)
