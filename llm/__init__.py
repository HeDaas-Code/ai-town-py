"""LLM 客户端包：OpenAI 兼容的 chat / embedding 接口。"""
from .client import LLMClient, LLMMessage
from .offline import OfflineLLMClient

__all__ = ["LLMClient", "LLMMessage", "OfflineLLMClient"]
