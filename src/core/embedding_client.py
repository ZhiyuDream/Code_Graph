"""统一 Embedding 编码接口。

设计原则：
- 所有向量编码走这里，业务代码不再直接创建 OpenAI client。
- 新增后端（本地 BGE、sentence-transformers、onnx、vLLM embedding 等）只需实现
  EmbeddingEncoder.encode()，并在 get_encoder() 中注册。
- 维度、batch、归一化由各后端自行保证；返回统一为 np.ndarray(float32)。
"""
from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

# 延迟导入 openai，避免本地后端无需 openai 时强依赖
_OPENAI_AVAILABLE = False
try:
    from openai import OpenAI
    _OPENAI_AVAILABLE = True
except ImportError:  # pragma: no cover
    pass


class EmbeddingEncoder(ABC):
    """Embedding 编码器抽象基类。"""

    @abstractmethod
    def encode(self, texts: list[str]) -> np.ndarray:
        """
        将文本列表编码为向量矩阵。

        Args:
            texts: 待编码文本列表。

        Returns:
            shape=(len(texts), dim) 的 float32 numpy 数组。
        """
        ...

    def encode_single(self, text: str) -> np.ndarray:
        """单个文本编码的便捷方法。"""
        return self.encode([text])[0]


class OpenAIEmbeddingEncoder(EmbeddingEncoder):
    """OpenAI 或 OpenAI 兼容 API 的 Embedding 后端。"""

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        client: Any | None = None,
    ):
        if not _OPENAI_AVAILABLE:
            raise ImportError("openai package is required for OpenAIEmbeddingEncoder")

        from config import EMBEDDING_MODEL, OPENAI_API_KEY, OPENAI_BASE_URL

        self.model = model or EMBEDDING_MODEL
        self.api_key = api_key or OPENAI_API_KEY
        self.base_url = base_url if base_url is not None else (OPENAI_BASE_URL or None)
        self._client = client

    def _get_client(self) -> OpenAI:
        if self._client is None:
            if not self.api_key:
                raise ValueError(
                    "OpenAI API key not set. Please configure OPENAI_API_KEY in .env"
                )
            self._client = OpenAI(api_key=self.api_key, base_url=self.base_url or None)
        return self._client

    def encode(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)

        client = self._get_client()
        resp = client.embeddings.create(model=self.model, input=texts)
        embs = [None] * len(texts)
        for e in resp.data:
            embs[e.index] = e.embedding
        return np.asarray(embs, dtype=np.float32)


class LocalEmbeddingEncoder(EmbeddingEncoder):
    """
    本地 Embedding 后端占位实现。

    可扩展为：
    - 调用本地 HTTP 服务（如 sentence-transformers + FastAPI）
    - 直接加载 onnx / torch 模型
    - 调用 vLLM / Ollama 的 embedding 端点

    默认实现通过 HTTP POST 调用本地服务，约定请求/响应格式：
      POST {endpoint}/encode
      body: {"texts": ["...", "..."]}
      response: {"embeddings": [[...], [...]]}
    你可以根据需要修改为本项目统一的本地调用方式。
    """

    def __init__(self, endpoint: str | None = None, model_path: str | None = None):
        """
        Args:
            endpoint: 本地 embedding 服务地址，例如 http://localhost:8000。
            model_path: 本地模型路径（若直接加载模型时使用）。
        """
        self.endpoint = endpoint
        self.model_path = model_path

    def encode(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)

        # 优先走 HTTP 服务
        if self.endpoint:
            import requests
            url = self.endpoint.rstrip("/") + "/encode"
            resp = requests.post(url, json={"texts": texts}, timeout=120)
            resp.raise_for_status()
            embs = resp.json()["embeddings"]
            return np.asarray(embs, dtype=np.float32)

        # 若提供 model_path，可在此加载本地模型；默认给出明确错误
        if self.model_path:
            raise NotImplementedError(
                "Direct local model loading is not implemented yet. "
                "Please provide an HTTP endpoint or subclass LocalEmbeddingEncoder."
            )

        raise ValueError(
            "LocalEmbeddingEncoder requires either 'endpoint' or 'model_path'."
        )


def get_encoder(model: str | None = None, backend: str | None = None) -> EmbeddingEncoder:
    """
    获取默认或指定模型的 EmbeddingEncoder。

    Args:
        model: 模型名称。留空使用 config.EMBEDDING_MODEL。
        backend: 强制指定后端，如 "openai" / "local"。留空则根据环境推断。

    Returns:
        EmbeddingEncoder 实例。
    """
    from config import EMBEDDING_MODEL, OPENAI_API_KEY, OPENAI_BASE_URL

    model = model or EMBEDDING_MODEL

    # 环境变量可强制切换后端，方便本地部署时无需改代码
    backend = backend or os.environ.get("EMBEDDING_BACKEND", "auto")

    if backend == "local" or os.environ.get("LOCAL_EMBEDDING_ENDPOINT"):
        endpoint = os.environ.get("LOCAL_EMBEDDING_ENDPOINT")
        model_path = os.environ.get("LOCAL_EMBEDDING_MODEL_PATH")
        return LocalEmbeddingEncoder(endpoint=endpoint, model_path=model_path)

    if backend == "openai" or backend == "auto":
        return OpenAIEmbeddingEncoder(
            model=model,
            api_key=OPENAI_API_KEY,
            base_url=OPENAI_BASE_URL or None,
        )

    raise ValueError(f"Unknown embedding backend: {backend}")
