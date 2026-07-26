"""OpenAI 兼容的 LLM 客户端。

移植自原项目 convex/util/llm.ts。所有 LLM 调用都走这里：
- ``chat_completion``：非流式聊天补全，支持 stop words / retry
- ``fetch_embedding``：文本嵌入向量（用于记忆检索）

支持任何 OpenAI 协议兼容的端点：OpenAI / Ollama / Together / 自建。
"""
from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from config import LLMConfig, get_llm_config


# 退避策略：第 i 次失败后等 RETRY_BACKOFF[i] ms（+ jitter）
RETRY_BACKOFF_MS = [1000, 10000, 20000]
RETRY_JITTER_MS = 100


@dataclass
class LLMMessage:
    """OpenAI 风格的 chat 消息。"""

    role: str  # 'system' | 'user' | 'assistant' | 'function'
    content: Optional[str]
    name: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"role": self.role, "content": self.content}
        if self.name is not None:
            d["name"] = self.name
        return d


@dataclass
class ChatResult:
    content: str
    retries: int
    ms: float


@dataclass
class EmbeddingResult:
    embedding: List[float]
    retries: int
    ms: float


class LLMError(Exception):
    """LLM 调用失败。``retryable`` 指示是否值得重试。"""

    def __init__(self, message: str, *, retryable: bool = False, status: int = 0):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


class LLMClient:
    """薄封装：从 ``LLMConfig`` 构造，提供 chat / embedding 方法。"""

    def __init__(self, config: Optional[LLMConfig] = None):
        self.config = config or get_llm_config()

    # ---- chat ----
    def chat_completion(
        self,
        messages: List[LLMMessage],
        *,
        max_tokens: int = 300,
        temperature: Optional[float] = None,
        stop: Optional[List[str]] = None,
        response_format: Optional[Dict[str, str]] = None,
    ) -> ChatResult:
        body: Dict[str, Any] = {
            "model": self.config.chat_model,
            "messages": [m.to_dict() for m in messages],
            "max_tokens": max_tokens,
        }
        if temperature is not None:
            body["temperature"] = temperature
        # 合并 stop words：调用方 + 全局（如 Ollama 的 <|eot_id|>）
        stop_words: List[str] = list(stop or [])
        if self.config.stop_words:
            stop_words.extend(self.config.stop_words)
        if stop_words:
            body["stop"] = stop_words
        if response_format is not None:
            body["response_format"] = response_format

        result = self._retry(self._post_json, f"{self.config.url}/v1/chat/completions", body)
        json_resp = result["value"]
        choices = json_resp.get("choices") or []
        if not choices:
            raise LLMError(f"Unexpected result from LLM: {json.dumps(json_resp)}")
        content = (choices[0].get("message") or {}).get("content")
        if content is None:
            raise LLMError(f"No content in LLM response: {json.dumps(json_resp)}")
        return ChatResult(content=content, retries=result["retries"], ms=result["ms"])

    # ---- embedding ----
    def fetch_embedding(self, text: str) -> EmbeddingResult:
        # Ollama 用 /api/embeddings，OpenAI 风格用 /v1/embeddings
        if self.config.provider == "ollama":
            body = {"model": self.config.embedding_model, "prompt": text.replace("\n", " ")}
            result = self._retry(self._post_json, f"{self.config.url}/api/embeddings", body)
            embedding = result["value"].get("embedding")
            if not embedding:
                raise LLMError(f"No embedding in Ollama response: {result['value']}")
            return EmbeddingResult(embedding=embedding, retries=result["retries"], ms=result["ms"])

        body = {
            "model": self.config.embedding_model,
            "input": [text.replace("\n", " ")],
        }
        result = self._retry(self._post_json, f"{self.config.url}/v1/embeddings", body)
        data = result["value"].get("data") or []
        if not data:
            raise LLMError(f"No embedding in response: {result['value']}")
        # 按 index 排序后取第一条
        data.sort(key=lambda d: d.get("index", 0))
        return EmbeddingResult(
            embedding=data[0]["embedding"], retries=result["retries"], ms=result["ms"]
        )

    # ---- 内部：HTTP + 重试 ----
    def _auth_headers(self) -> Dict[str, str]:
        if self.config.api_key:
            return {"Authorization": f"Bearer {self.config.api_key}"}
        return {}

    def _post_json(self, url: str, body: Dict[str, Any]) -> Dict[str, Any]:
        headers = {"Content-Type": "application/json", **self._auth_headers()}
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            text = ""
            try:
                text = e.read().decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                pass
            retryable = e.code == 429 or e.code >= 500
            raise LLMError(
                f"LLM request failed ({e.code}): {text}", retryable=retryable, status=e.code
            )
        except urllib.error.URLError as e:
            raise LLMError(f"LLM request URL error: {e}", retryable=True)

    def _retry(self, fn, *args, **kwargs) -> Dict[str, Any]:
        """带退避的重试：仅当 LLMError.retryable=True 时重试。"""
        last_error: Optional[Exception] = None
        for i in range(len(RETRY_BACKOFF_MS) + 1):
            start = time.time()
            try:
                value = fn(*args, **kwargs)
                ms = (time.time() - start) * 1000.0
                return {"value": value, "retries": i, "ms": ms}
            except LLMError as e:
                last_error = e
                if not e.retryable or i >= len(RETRY_BACKOFF_MS):
                    raise
                sleep_ms = RETRY_BACKOFF_MS[i] + int(RETRY_JITTER_MS * random.random())
                time.sleep(sleep_ms / 1000.0)
            except Exception as e:  # noqa: BLE001
                last_error = e
                if i >= len(RETRY_BACKOFF_MS):
                    raise
                sleep_ms = RETRY_BACKOFF_MS[i] + int(RETRY_JITTER_MS * random.random())
                time.sleep(sleep_ms / 1000.0)
        # 不可达
        raise last_error  # type: ignore[misc]


# 模块级单例，方便全局复用
_default_client: Optional[LLMClient] = None


def get_default_client() -> LLMClient:
    global _default_client
    if _default_client is None:
        _default_client = LLMClient()
    return _default_client


def reset_default_client() -> None:
    """测试用：重置单例以便切换 LLM 配置。"""
    global _default_client
    _default_client = None
