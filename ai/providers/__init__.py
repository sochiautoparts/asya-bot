"""AI Providers — Pollinations PRIMARY + Local FALLBACK (optional)."""

from ai.providers.base import BaseAIProvider, AIResponse
from ai.providers.pollinations_provider import PollinationsProvider

# Conditional import — only needed when ENABLE_LOCAL_MODEL=true
try:
    from ai.providers.llama_cpp_provider import LlamaCppProvider
    _LLAMA_CPP_AVAILABLE = True
except ImportError:
    LlamaCppProvider = None
    _LLAMA_CPP_AVAILABLE = False

ALL_PROVIDERS = {
    "pollinations": PollinationsProvider,
}
if _LLAMA_CPP_AVAILABLE:
    ALL_PROVIDERS["llama_cpp"] = LlamaCppProvider
