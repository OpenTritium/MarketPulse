"""Embedding 层：本地 fastembed + BAAI/bge-base-zh-v1.5（768 维）。

首次运行会自动从 HuggingFace 下载 ONNX 模型（~400MB，需代理）。
"""

from __future__ import annotations
import threading
from typing import Any
from fastembed import TextEmbedding
from .config import Config


class Embedder:
    _lock: threading.Lock = threading.Lock()

    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg: Config = cfg
        self._model: TextEmbedding | None = None
        self._dim: int = cfg.embedding_dim

    def _ensure(self) -> TextEmbedding:
        """惰性加载模型（首次调用触发下载）。"""
        if self._model is None:
            with self._lock:
                if self._model is None:
                    self._model = TextEmbedding(model_name=self.cfg.embedding_model)
        return self._model

    @staticmethod
    def _to_float(x: Any) -> float:
        try:
            return float(x)
        except (TypeError, ValueError):
            return 0.0

    def embed(self, text: str) -> list[float]:
        """单条文本 → 512 维向量。空文本返回全零向量。"""
        if not text or not text.strip():
            return [0.0] * self._dim
        model = self._ensure()
        vec = next(iter(model.embed([text[:2000]])))
        return [self._to_float(x) for x in vec]
