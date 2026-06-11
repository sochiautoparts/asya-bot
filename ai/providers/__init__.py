"""AI Providers — Local (Qwen3-4B) + Pollinations (key + free) + Cloudflare Workers AI."""

from ai.providers.base import BaseAIProvider, AIResponse
from ai.providers.local_provider import LocalProvider
from ai.providers.pollinations_provider import PollinationsProvider
from ai.providers.cloudflare_provider import CloudflareProvider

ALL_PROVIDERS = {
    "local": LocalProvider,
    "pollinations": PollinationsProvider,
    "cloudflare": CloudflareProvider,
}
