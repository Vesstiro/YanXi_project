"""PythonProject2 的字符 n-gram + Qwen/Chroma 混合规则检索。"""

from .embeddings import QwenEmbeddings
from .retriever import RagRetriever

__all__ = ["QwenEmbeddings", "RagRetriever"]
