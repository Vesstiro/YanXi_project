"""Qwen 的真实向量接口；不导入旧项目、不读取旧 .env、不隐式联网。"""

from __future__ import annotations

import math
from numbers import Real
from typing import Any, Sequence


DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


class EmbeddingServiceError(RuntimeError):
    """可以直接显示给用户、不包含请求密钥的向量服务错误。"""


def validate_vectors(
    vectors: Sequence[Sequence[float]], expected_count: int, dimensions: int
) -> list[list[float]]:
    """拒绝截断、错误维度和 NaN/Inf，避免把坏向量持久化。"""
    if len(vectors) != expected_count:
        raise EmbeddingServiceError("Qwen 向量响应条数不完整，未保存本批向量。")
    checked = []
    for vector in vectors:
        if len(vector) != dimensions:
            raise EmbeddingServiceError("Qwen 向量响应维度与配置不一致，未保存本批向量。")
        row = []
        for value in vector:
            if isinstance(value, bool) or not isinstance(value, Real):
                raise EmbeddingServiceError("Qwen 向量响应包含非数值元素，未保存本批向量。")
            number = float(value)
            if not math.isfinite(number):
                raise EmbeddingServiceError("Qwen 向量响应包含 NaN/Inf，未保存本批向量。")
            row.append(number)
        checked.append(row)
    return checked


class QwenEmbeddings:
    """兼容 Chroma/LangChain 的 duck-typed Embeddings 接口。

    构造函数仅保存配置。首次 embed 才导入 OpenAI SDK/创建客户端；注入的
    client 可用于离线测试或复用当前工程的 Qwen 客户端，close 不会关闭它。
    """

    def __init__(
        self,
        client: Any = None,
        *,
        api_key: str = "",
        base_url: str = DEFAULT_BASE_URL,
        model: str = "text-embedding-v4",
        dimensions: int = 1024,
        batch_size: int = 10,
        timeout: float = 60.0,
    ):
        if isinstance(dimensions, bool) or not isinstance(dimensions, int) or dimensions < 1:
            raise ValueError("embedding_dimensions 必须是正整数。")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("embedding_model 不能为空。")
        self.client = client
        self.api_key = str(api_key or "").strip()
        self.base_url = str(base_url or DEFAULT_BASE_URL).strip().rstrip("/")
        self.model = model.strip()
        self.dimensions = dimensions
        self.batch_size = max(1, min(int(batch_size), 10))
        self.timeout = float(timeout)
        self._owns_client = False

    def _get_client(self):
        if self.client is None:
            if not self.api_key:
                raise EmbeddingServiceError(
                    "RAG 尚未配置 Qwen 向量 API Key。请在本工程设置中填写 Qwen 密钥；"
                    "聊天模型的其他厂商密钥不能用于 Qwen embedding。"
                )
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise EmbeddingServiceError("缺少 openai 依赖，请安装本工程 requirements-desktop.txt。") from exc
            self.client = OpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=self.timeout,
                max_retries=2,
            )
            self._owns_client = True
        return self.client

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        client = self._get_client()
        try:
            response = client.embeddings.create(
                model=self.model,
                input=texts,
                dimensions=self.dimensions,
                encoding_format="float",
                timeout=self.timeout,
            )
        except Exception as exc:
            # SDK 异常可能包含 URL/请求字段，不把原异常文本放入 GUI/评测报告。
            raise EmbeddingServiceError(
                "Qwen 向量 API 调用失败，请检查密钥、地域、embedding 模型权限和网络。"
            ) from exc
        try:
            data = response.data
            if len(data) != len(texts):
                raise EmbeddingServiceError("Qwen 向量响应条数不完整，未保存本批向量。")
            indexes = [item.index for item in data]
            if (
                any(isinstance(index, bool) or not isinstance(index, int) for index in indexes)
                or sorted(indexes) != list(range(len(texts)))
            ):
                raise EmbeddingServiceError("Qwen 向量响应索引缺失或重复，未保存本批向量。")
            vectors = [item.embedding for item in sorted(data, key=lambda item: item.index)]
            return validate_vectors(vectors, len(texts), self.dimensions)
        except EmbeddingServiceError:
            raise
        except (AttributeError, TypeError, ValueError, OverflowError) as exc:
            raise EmbeddingServiceError("Qwen 向量响应格式无效，未保存本批向量。") from exc

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if any(not isinstance(text, str) for text in texts):
            raise ValueError("向量输入必须是文本列表。")
        vectors = []
        for start in range(0, len(texts), self.batch_size):
            vectors.extend(self._embed_batch(texts[start : start + self.batch_size]))
        return vectors

    def embed_query(self, text: str) -> list[float]:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("检索文本不能为空。")
        return self._embed_batch([text])[0]

    def close(self):
        if self._owns_client and self.client is not None:
            self.client.close()
            self.client = None
            self._owns_client = False
