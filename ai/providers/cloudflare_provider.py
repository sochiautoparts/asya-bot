"""Cloudflare Workers AI Provider — @cf/mistralai/mistral-small-3.1-24b-instruct

Multi-account Cloudflare Workers AI provider with:
  - TWO accounts with automatic rotation (each 10K req/day)
  - OpenAI-compatible vision format (image_url in content array)
  - REST API via /client/v4/accounts/{id}/ai/run/ endpoint
  - Per-account daily request tracking and rotation
  - Circuit breaking for failed accounts

FAILOVER CHAIN (within provider):
  Account 1 → Account 2 → Error (router decides next fallback)

VISION FORMAT:
  Standard OpenAI-compatible content array with image_url type.
  Must use native /ai/run/ endpoint (NOT /v1/chat/completions).
"""

import httpx
import json
import logging
import time
from typing import Optional, List, Dict, Tuple
from dataclasses import dataclass

from ai.providers.base import BaseAIProvider, AIResponse
from bot.config import config

logger = logging.getLogger("asya.ai.cloudflare")

# ── Cloudflare Workers AI Model ──
CF_MODEL = "@cf/mistralai/mistral-small-3.1-24b-instruct"

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
                return True
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

    def _build_url(self, account: CFAccount) -> str:
        """Build the API URL for a given account."""
        return (
            f"{self.base_url}/accounts/{account.account_id}/ai/run/{CF_MODEL}"
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

        Tries accounts in rotation: Account 1 → Account 2 → Error.
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

            try:
                url = self._build_url(account)
                headers = self._build_headers(account)

                payload = {
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                }

                async with httpx.AsyncClient(timeout=60.0) as client:
                    start_time = time.time()
                    response = await client.post(url, headers=headers, json=payload)
                    elapsed = time.time() - start_time

                    if response.status_code == 200:
                        data = response.json()

                        # Cloudflare response format: { "result": { "response": "..." } }
                        # Or OpenAI-compatible: { "choices": [...] }
                        text = self._extract_text(data)

                        if not text:
                            logger.warning(
                                f"CF Account {account.index}: empty response, "
                                f"elapsed={elapsed:.1f}s"
                            )
                            account.mark_depleted("Empty response")
                            self._rotate_account()
                            account = self._get_active_account()
                            continue

                        account.increment()
                        logger.info(
                            f"CF response (Account {account.index}): "
                            f"model={CF_MODEL}, time={elapsed:.1f}s, "
                            f"length={len(text)}, requests_today={account.request_count}/{DAILY_REQUEST_LIMIT}"
                        )

                        return AIResponse(
                            text=text,
                            model=CF_MODEL,
                            provider=self.name,
                        )

                    elif response.status_code in (401, 403):
                        error_text = response.text[:300]
                        logger.error(
                            f"CF Account {account.index}: auth error {response.status_code}: {error_text}"
                        )
                        account.mark_depleted(f"HTTP {response.status_code}")
                        self._rotate_account()
                        account = self._get_active_account()
                        continue

                    elif response.status_code == 429:
                        # Rate limited
                        logger.warning(f"CF Account {account.index}: rate limited (429)")
                        account.mark_depleted("Rate limited")
                        self._rotate_account()
                        account = self._get_active_account()
                        continue

                    elif response.status_code == 500:
                        # Server error — might be model overload
                        error_text = response.text[:300]
                        logger.error(
                            f"CF Account {account.index}: server error 500: {error_text}"
                        )
                        # Don't mark as depleted for 500s — could be transient
                        self._rotate_account()
                        account = self._get_active_account()
                        continue

                    else:
                        error_text = response.text[:300]
                        logger.error(
                            f"CF Account {account.index}: HTTP {response.status_code}: {error_text}"
                        )
                        return AIResponse(
                            text="",
                            model=CF_MODEL,
                            provider=self.name,
                            error=True,
                            error_message=f"HTTP {response.status_code}: {error_text}",
                        )

            except httpx.TimeoutException:
                logger.error(f"CF Account {account.index}: request timeout")
                self._rotate_account()
                account = self._get_active_account()
                continue

            except Exception as e:
                logger.error(f"CF Account {account.index}: exception: {e}")
                self._rotate_account()
                account = self._get_active_account()
                continue

        # All accounts failed
        return AIResponse(
            text="",
            model=CF_MODEL,
            provider=self.name,
            error=True,
            error_message="All Cloudflare accounts failed",
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

    async def generate_image(self, prompt: str, model: str = "flux") -> Optional[bytes]:
        """Cloudflare Workers AI does NOT support image generation via this model.
        Returns None — caller should fall back to another provider.
        """
        logger.debug("CloudflareProvider: image generation not supported, returning None")
        return None

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
