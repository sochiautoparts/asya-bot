"""AI Providers — Pollinations (key + free) + Cloudflare Workers AI."""

from ai.providers.base import BaseAIProvider, AIResponse
from ai.providers.pollinations_provider import PollinationsProvider
from ai.providers.cloudflare_provider import CloudflareProvider

ALL_PROVIDERS = {
    "pollinations": PollinationsProvider,
    "cloudflare": CloudflareProvider,
}
