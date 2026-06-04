"""
Base AI Provider — Abstract interface for LLM providers.
"""

import abc
from typing import Optional, List, Dict, Any
from dataclasses import dataclass


@dataclass
class AIResponse:
    """Response from an AI provider."""
    text: str
    model: str
    provider: str
    tokens_used: int = 0
    cached: bool = False
    error: bool = False
    error_message: str = ""


class BaseAIProvider(abc.ABC):
    """Abstract base class for AI providers."""

    def __init__(self, name: str, api_key: str = "", base_url: str = ""):
        self.name = name
        self.api_key = api_key
        self.base_url = base_url
        self._available = True

    @abc.abstractmethod
    async def chat(
        self,
        messages: List[Dict[str, str]],
        model: str = "",
        temperature: float = 0.7,
        max_tokens: int = 2048,
        **kwargs,
    ) -> AIResponse:
        """Send a chat completion request."""
        pass

    @abc.abstractmethod
    async def is_available(self) -> bool:
        """Check if the provider is currently available."""
        pass

    def format_messages(
        self,
        system_prompt: str,
        history: List[Dict[str, Any]],
        user_message: str,
    ) -> List[Dict[str, str]]:
        """Format messages into OpenAI-compatible format."""
        messages = [{"role": "system", "content": system_prompt}]

        for msg in history:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})

        messages.append({"role": "user", "content": user_message})
        return messages
