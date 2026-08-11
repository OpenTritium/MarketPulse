"""Embedding 层：固定使用本地 bge-small-zh-v1.5（512 维）。

首次运行会自动从 HuggingFace 下载 ONNX 模型。
"""

from __future__ import annotations

import threading
from typing import Any

from fastembed import TextEmbedding

from .config import EMBEDDING_DIM, EMBEDDING_MODEL, Config


class Embedder:
    _lock: threading.Lock = threading.Lock()

    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg: Config = cfg
        self._model: TextEmbedding | None = None
        self._embed_lock: threading.Lock = threading.Lock()

    def _ensure(self) -> TextEmbedding:
        """惰性加载模型（首次调用触发下载）。"""
        if self._model is None:
            with self._lock:
                if self._model is None:
                    self._model = TextEmbedding(model_name=EMBEDDING_MODEL)
        return self._model

    @staticmethod
    def _to_float(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def embed(self, text: str) -> list[float]:
        """单条文本 → 固定 512 维向量。"""
        return self.embed_many([text])[0]

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        """批量向量化文本，并验证模型输出与存储契约一致。"""
        vectors = [[0.0] * EMBEDDING_DIM for _ in texts]
        pending = [
            (index, text[:2000]) for index, text in enumerate(texts) if text.strip()
        ]
        if not pending:
            return vectors

        model = self._ensure()
        with self._embed_lock:
            embeddings = list(model.embed([text for _, text in pending]))
        for (index, _), embedding in zip(pending, embeddings, strict=True):
            vector = [self._to_float(value) for value in embedding]
            if len(vector) != EMBEDDING_DIM:
                raise RuntimeError(
                    f"embedding 维度错误：期望 {EMBEDDING_DIM}，实际 {len(vector)}"
                )
            vectors[index] = vector
        return vectors
