"""离线 / 占位 LLM 客户端。

当 ``AppConfig.enable_llm=False`` 时使用，不调用任何外部 API，
返回固定的占位文本，便于在没有 LLM 的环境下跑通整个引擎流程。
"""
from __future__ import annotations

import hashlib
import random
from typing import List

from .client import ChatResult, EmbeddingResult, LLMClient, LLMMessage


class OfflineLLMClient(LLMClient):
    """不联网的 LLM 客户端：chat 返回占位文本，embedding 返回确定性哈希向量。"""

    def __init__(self, dimension: int = 64):
        # 不调父类的网络配置；直接跳过 get_llm_config
        from config import LLMConfig
        self.config = LLMConfig(
            provider="offline",
            url="",
            chat_model="offline-placeholder",
            embedding_model="offline-placeholder",
            stop_words=[],
            api_key=None,
            embedding_dimension=dimension,
        )
        self._dimension = dimension

    def chat_completion(
        self,
        messages: List[LLMMessage],
        *,
        max_tokens: int = 300,
        temperature: float | None = None,
        stop: List[str] | None = None,
        response_format=None,
    ) -> ChatResult:
        # 从 system+user 提示里拼一个看起来合理的回复
        last_user = next((m for m in reversed(messages) if m.role == "user"), None)
        seed_text = (last_user.content if last_user and last_user.content else "Hello!")[:32]
        variants = [
            f"Hi there! {seed_text[:16]}... interesting.",
            f"I was just thinking about that. {seed_text[:16]}.",
            "Hmm, that's a good point. Tell me more.",
            "I'd love to chat, but I should get going. See you!",
        ]
        rng = random.Random(seed_text)
        return ChatResult(content=rng.choice(variants), retries=0, ms=0.0)

    def fetch_embedding(self, text: str) -> EmbeddingResult:
        # 用 sha256 哈希把文本映射成固定维度的伪向量
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        # 重复 digest 拼到目标维度
        bytes_needed = self._dimension * 4  # 每个 float 用 4 字节
        buf = (digest * (bytes_needed // len(digest) + 1))[:bytes_needed]
        embedding = [
            (int.from_bytes(buf[i * 4 : i * 4 + 4], "big") % 1000) / 1000.0
            for i in range(self._dimension)
        ]
        return EmbeddingResult(embedding=embedding, retries=0, ms=0.0)
