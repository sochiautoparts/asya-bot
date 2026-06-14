"""Hugging Face Spaces image generation provider for asya-bot.

Uses free Hugging Face Spaces API for image generation.
Multiple model endpoints for failover:
1. black-forest-labs/FLUX.1-schnell — fast, free
2. stabilityai/stable-diffusion-xl-base-1.0 — SDXL
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import time
from typing import Any, Optional, List, Dict

import httpx

from .base import AIResponse, BaseAIProvider

logger = logging.getLogger("asya.ai.huggingface")

# ── Free Hugging Face Spaces for image generation ──
HF_IMAGE_ENDPOINTS = [
    {
        "name": "FLUX-schnell",
        "url": "https://black-forest-labs-flux-1-schnell.hf.space",
        "api_path": "/api/predict",
    },
    {
        "name": "stable-diffusion-xl",
        "url": "https://stabilityai-stable-diffusion-xl-base-1-0.hf.space",
        "api_path": "/api/predict",
    },
]

HF_INFERENCE_API = "https://api-inference.huggingface.co/models"


class HuggingFaceProvider(BaseAIProvider):
    """Hugging Face Spaces provider for free image generation.

    Uses multiple free endpoints with failover:
    1. Hugging Face Inference API (free tier, no key needed for some models)
    2. Gradio Spaces API (free, no key needed)
    """

    def __init__(self, api_key: str = "", **kwargs: Any) -> None:
        super().__init__(name="huggingface", api_key=api_key, **kwargs)
        self._request_count = 0
        self._error_count = 0

    async def chat(
        self,
        messages: List[Dict[str, str]],
        model: str = "",
        temperature: float = 0.7,
        max_tokens: int = 2048,
        **kwargs,
    ) -> AIResponse:
        """Not supported — this provider is image-only."""
        return AIResponse(
            text="",
            model=model,
            provider=self.name,
            error="HuggingFace provider is image-only",
        )

    async def generate_image(
        self,
        prompt: str,
        width: int = 1024,
        height: int = 1024,
        model: str = "",
        **kwargs,
    ) -> AIResponse:
        """Generate image using Hugging Face free endpoints."""
        start = time.monotonic()

        # 1. Try HF Inference API (free models, no key for some)
        result = await self._try_inference_api(prompt, model)
        if result and result.ok:
            return result

        # 2. Try Gradio Spaces
        for endpoint in HF_IMAGE_ENDPOINTS:
            result = await self._try_gradio_space(prompt, endpoint)
            if result and result.ok:
                return result

        return AIResponse(
            text="",
            model=model,
            provider=self.name,
            error="HuggingFace image generation failed on all endpoints",
            latency_ms=(time.monotonic() - start) * 1000,
        )

    async def _try_inference_api(
        self, prompt: str, model: str = ""
    ) -> Optional[AIResponse]:
        """Try Hugging Face Inference API for image generation."""
        models = [
            "stabilityai/stable-diffusion-xl-base-1.0",
            "runwayml/stable-diffusion-v1-5",
            "stabilityai/stable-diffusion-2-1",
        ]

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        for hf_model in models:
            url = f"{HF_INFERENCE_API}/{hf_model}"
            payload = {"inputs": prompt}

            try:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    resp = await client.post(url, json=payload, headers=headers)
                    
                    if resp.status_code == 200:
                        content_type = resp.headers.get("content-type", "")
                        if "image" in content_type:
                            img_bytes = resp.content
                            if len(img_bytes) > 5000:
                                img_b64 = base64.b64encode(img_bytes).decode("utf-8")
                                self._request_count += 1
                                logger.info(
                                    "HF Inference API success: model=%s, %d bytes",
                                    hf_model, len(img_bytes),
                                )
                                return AIResponse(
                                    text="",
                                    image_b64=img_b64,
                                    model=hf_model,
                                    provider=self.name,
                                )
                    elif resp.status_code == 503:
                        # Model is loading — wait and retry once
                        logger.debug("HF model %s loading, waiting...", hf_model)
                        await asyncio.sleep(15)
                        async with httpx.AsyncClient(timeout=60.0) as client2:
                            resp2 = await client2.post(url, json=payload, headers=headers)
                            if resp2.status_code == 200:
                                ct2 = resp2.headers.get("content-type", "")
                                if "image" in ct2:
                                    img_bytes = resp2.content
                                    if len(img_bytes) > 5000:
                                        img_b64 = base64.b64encode(img_bytes).decode("utf-8")
                                        self._request_count += 1
                                        return AIResponse(
                                            text="",
                                            image_b64=img_b64,
                                            model=hf_model,
                                            provider=self.name,
                                        )
                    elif resp.status_code == 429:
                        logger.debug("HF Inference API rate limited for %s", hf_model)
                        continue
                    else:
                        logger.debug("HF Inference API error %d for %s", resp.status_code, hf_model)
                        continue
            except Exception as exc:
                logger.debug("HF Inference API request failed for %s: %s", hf_model, exc)
                continue

        return None

    async def _try_gradio_space(
        self, prompt: str, endpoint: dict
    ) -> Optional[AIResponse]:
        """Try a Gradio Space API endpoint for image generation."""
        url = f"{endpoint['url']}{endpoint['api_path']}"
        payload = {"data": [prompt]}

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(url, json=payload)
                if resp.status_code == 200:
                    data = resp.json()
                    if "data" in data and data["data"]:
                        result = data["data"][0]
                        if isinstance(result, str):
                            if result.startswith("http"):
                                try:
                                    img_resp = await client.get(result)
                                    if img_resp.status_code == 200:
                                        img_bytes = img_resp.content
                                        if len(img_bytes) > 5000:
                                            img_b64 = base64.b64encode(img_bytes).decode("utf-8")
                                            self._request_count += 1
                                            return AIResponse(
                                                text="",
                                                image_b64=img_b64,
                                                model=endpoint["name"],
                                                provider=self.name,
                                            )
                                except Exception:
                                    pass
                            elif result.startswith("data:image"):
                                parts = result.split(",", 1)
                                if len(parts) == 2:
                                    img_b64 = parts[1]
                                    if len(img_b64) > 1000:
                                        self._request_count += 1
                                        return AIResponse(
                                            text="",
                                            image_b64=img_b64,
                                            model=endpoint["name"],
                                            provider=self.name,
                                        )
                elif resp.status_code == 429:
                    logger.debug("Gradio Space %s rate limited", endpoint["name"])
        except Exception as exc:
            logger.debug("Gradio Space %s failed: %s", endpoint["name"], exc)

        return None

    async def is_available(self) -> bool:
        """Always available — free tier."""
        return True

    def get_status(self) -> Dict[str, Any]:
        return {
            "provider": self.name,
            "requests": self._request_count,
            "errors": self._error_count,
            "available": True,
        }
