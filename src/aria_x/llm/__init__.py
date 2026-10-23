"""LLM provider abstraction (PentAGI-inspired, dependency-free)."""

from .base import ChatMessage, LLMProvider, LLMResponse, ToolCall, ToolSpecPayload
from .mock import MockProvider
from .router import ModelRouter

__all__ = ["ChatMessage", "LLMProvider", "LLMResponse", "ToolCall", "ToolSpecPayload",
           "MockProvider", "ModelRouter"]
