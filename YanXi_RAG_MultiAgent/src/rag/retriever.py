"""移植 PythonProject2 RAGRetriever 的真实 n-gram + 向量融合算法。

索引以知识内容、Qwen 地域/模型/维度为指纹分目录保存，不删除旧缓存。
评测可为每例指定独立 chroma_persist_dir；只有不可变知识向量能够在同一
进程内复用，来电查询、客户端和业务状态不进入共享缓存。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .embeddings import DEFAULT_BASE_URL, EmbeddingServiceError, QwenEmbeddings, validate_vectors


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class RagRetrievalError(RuntimeError):
    """检索不能完整执行；禁止伪装成只用 n-gram 的正常结果。"""


@dataclass(frozen=True)
class KnowledgeDocument:
    page_content: str
    metadata: dict[str, str]


class RagRetriever:
    # 最多保留四个知识版本的纯数字向量；不保留 client/query/来电个人信息。
    _document_cache: OrderedDict[str, tuple[tuple[float, ...], ...]] = OrderedDict()
    _cache_lock = threading.RLock()
    _cache_limit = 4

    def __init__(self, config: dict, llm_client=None, vector_db=None):
        rag = config.get("rag", {})
        qwen = config.get("llm", {}).get("qwen", {})
        self.knowledge_path = self._project_path(rag.get("knowledge_path", "assets/knowledge.txt"))
        self.persist_root = self._project_path(rag.get("chroma_persist_dir", "data/chroma_db"))
        self.embedding_model = rag.get("embedding_model", "text-embedding-v4")
        self.embedding_dimensions = rag.get("embedding_dimensions", 1024)
        self.collection_name = rag.get("collection_name", "yanxi_qwen_rules")
        self.threshold = float(rag.get("similarity_threshold", 0.90))
        if not math.isfinite(self.threshold) or self.threshold <= 0:
            raise ValueError("rag.similarity_threshold 必须是有限正数。")
        self.last_evidence: list[dict] = []
        self.vector_db = vector_db
        self._injected_vector_db = vector_db is not None
        self._closed = False
        self.diagnostics: dict[str, Any] = {
            "knowledge_path": str(self.knowledge_path),
            "retrieval_mode": "ngram+qwen-vector",
            "embedding_model": self.embedding_model,
            "embedding_dimensions": self.embedding_dimensions,
            "cache_status": "injected" if self._injected_vector_db else "not_initialized",
            "document_embedding_cache": "not_used",
        }
        client = self._qwen_client(llm_client)
        # 只读取调用方明确指定的当前进程变量；不扫描/加载任意旧工程 .env。
        api_key = qwen.get("api_key", "")
        if not api_key and qwen.get("api_key_env"):
            api_key = os.environ.get(str(qwen["api_key_env"]), "")
        self._embeddings = QwenEmbeddings(
            client=client,
            api_key=api_key,
            base_url=qwen.get("base_url", DEFAULT_BASE_URL),
            model=self.embedding_model,
            dimensions=self.embedding_dimensions,
            batch_size=10,
            timeout=qwen.get("timeout", 60),
        )
        self.documents, self._ids = self._load_knowledge()
        fingerprint_data = {
            "version": 1,
            "knowledge_sha256": self.knowledge_sha256,
            "base_url": self._embeddings.base_url,
            "embedding_model": self.embedding_model,
            "embedding_dimensions": self.embedding_dimensions,
            "distance": "l2",
        }
        self.fingerprint = hashlib.sha256(
            json.dumps(fingerprint_data, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        self.cache_dir = self.persist_root / ("index_" + self.fingerprint)
        collection_hash = hashlib.sha256(self.collection_name.encode("utf-8")).hexdigest()[:16]
        self._marker_path = self.cache_dir / ("complete_" + collection_hash + ".json")
        self.diagnostics.update(
            knowledge_sha256=self.knowledge_sha256,
            fingerprint=self.fingerprint,
            cache_dir=str(self.cache_dir),
        )
        self._ngram_index: dict[str, set[int]] = {}
        self._build_ngram_index()

    @staticmethod
    def _project_path(value) -> Path:
        path = Path(value).expanduser()
        return path.resolve() if path.is_absolute() else (PROJECT_ROOT / path).resolve()

    @staticmethod
    def _qwen_client(llm_client):
        if llm_client is None:
            return None
        # 不实例化另一个 LLMClient，不读取 PythonProject2 的配置或环境文件。
        for backend, info in getattr(llm_client, "_clients", {}).items():
            if getattr(backend, "value", backend) == "qwen":
                return info.get("client")
        return None

    def _load_knowledge(self) -> tuple[list[KnowledgeDocument], list[str]]:
        try:
            raw = self.knowledge_path.read_bytes()
            text = raw.decode("utf-8-sig")
        except (OSError, UnicodeError) as exc:
            raise RagRetrievalError("无法读取 UTF-8 知识文件，请检查 rag.knowledge_path。") from exc
        self.knowledge_sha256 = hashlib.sha256(raw).hexdigest()
        documents, ids, malformed = [], [], []
        blank_rows = 0
        for number, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line:
                blank_rows += 1
                continue
            parts = [part.strip() for part in line.split("|")]
            if len(parts) != 3 or not all(parts):
                malformed.append(number)
                continue
            documents.append(KnowledgeDocument(parts[0], {"call_type": parts[1], "handle_rule": parts[2]}))
            ids.append(hashlib.sha256(f"{number}\n{line}".encode("utf-8")).hexdigest())
        self.diagnostics.update(
            valid_rows=len(documents),
            skipped_rows=len(malformed),
            malformed_line_numbers=malformed,
            blank_rows=blank_rows,
        )
        if not documents:
            raise RagRetrievalError("知识文件没有有效规则；每行必须恰好为 内容|类型|处理规则 三列。")
        return documents, ids

    @staticmethod
    def _char_ngrams(text: str, n: int) -> set[str]:
        cleaned = re.sub(r"[^一-鿿0-9a-zA-Z]", "", text)
        return {cleaned[i : i + n] for i in range(max(0, len(cleaned) - n + 1))}

    def _build_ngram_index(self):
        for n in (2, 3, 4, 5):
            for index, document in enumerate(self.documents):
                for ngram in self._char_ngrams(document.page_content, n):
                    self._ngram_index.setdefault(f"{n}_{ngram}", set()).add(index)

    @classmethod
    def _ngram_match_score(cls, call_text: str, doc_text: str) -> float:
        total = 0.0
        for n, weight in ((2, 0.15), (3, 0.25), (4, 0.35), (5, 0.25)):
            caller = cls._char_ngrams(call_text, n)
            document = cls._char_ngrams(doc_text, n)
            if caller and document:
                total += len(caller & document) / max(len(caller), len(document)) * weight
        return total

    def _create_vector_db(self):
        # 缺依赖时 GUI 仍可启动；实际处理时明确报告，不能冒充完成混合检索。
        try:
            from chromadb.config import Settings
            from langchain_chroma import Chroma
        except ImportError as exc:
            raise RagRetrievalError(
                "缺少 Chroma 向量依赖，请安装本工程 requirements-desktop.txt 后重试。"
            ) from exc
        return Chroma(
            persist_directory=str(self.cache_dir),
            collection_name=self.collection_name,
            embedding_function=self._embeddings,
            collection_metadata={"hnsw:space": "l2"},
            client_settings=Settings(anonymized_telemetry=False),
        )

    def _existing_ids(self, database) -> set[str]:
        result = database._collection.get(ids=self._ids, include=["metadatas"])
        return set(result.get("ids", []))

    def _remember_vectors(self, vectors):
        # 只有完成整库写入并验证后才发布不可变缓存，绝不缓存失败的一部分。
        self._document_cache[self.fingerprint] = tuple(tuple(row) for row in vectors)
        self._document_cache.move_to_end(self.fingerprint)
        while len(self._document_cache) > self._cache_limit:
            self._document_cache.popitem(last=False)

    def _write_complete_marker(self):
        record = {
            "fingerprint": self.fingerprint,
            "knowledge_sha256": self.knowledge_sha256,
            "collection_name": self.collection_name,
            "document_count": len(self.documents),
            "complete": True,
        }
        temporary = self._marker_path.with_name(self._marker_path.name + "." + uuid.uuid4().hex + ".tmp")
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self._marker_path)

    def _ensure_vector_db(self):
        if self.vector_db is not None:
            return
        with self._cache_lock:
            if self.vector_db is not None:
                return
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            database = self._create_vector_db()
            existing = self._existing_ids(database)
            count = database._collection.count()
            if count != len(existing):
                raise RagRetrievalError(
                    "知识缓存包含不属于本版本的记录。旧数据未删除；请为 rag.chroma_persist_dir 指定新目录。"
                )
            missing = [index for index, identifier in enumerate(self._ids) if identifier not in existing]
            vectors = self._document_cache.get(self.fingerprint)
            if vectors is not None:
                self._document_cache.move_to_end(self.fingerprint)
                self.diagnostics["document_embedding_cache"] = "memory_hit"
            if missing:
                if vectors is None:
                    vectors = self._embeddings.embed_documents([doc.page_content for doc in self.documents])
                    self.diagnostics["document_embedding_cache"] = "computed"
                # 与原 Chroma.add_documents 等价，但显式传入完整、验证过的 Qwen
                # 文档向量，使隔离评测的每个索引不重复支付整份知识的向量化费用。
                for start in range(0, len(missing), 32):
                    batch = missing[start : start + 32]
                    database._collection.upsert(
                        ids=[self._ids[index] for index in batch],
                        documents=[self.documents[index].page_content for index in batch],
                        metadatas=[self.documents[index].metadata for index in batch],
                        embeddings=[list(vectors[index]) for index in batch],
                    )
                status = "recovered" if existing else "built"
            else:
                status = "loaded"
                if vectors is None:
                    saved = database._collection.get(ids=self._ids, include=["embeddings"])
                    saved_embeddings = saved.get("embeddings")
                    if saved_embeddings is None:
                        raise RagRetrievalError("完整知识缓存缺少向量，请使用新的缓存目录。")
                    by_id = dict(zip(saved["ids"], saved_embeddings))
                    vectors = validate_vectors(
                        [by_id[identifier] for identifier in self._ids],
                        len(self.documents),
                        self.embedding_dimensions,
                    )
                    self.diagnostics["document_embedding_cache"] = "disk_loaded"
            if (
                database._collection.count() != len(self.documents)
                or self._existing_ids(database) != set(self._ids)
            ):
                raise RagRetrievalError("知识向量库写入不完整；下次调用会检查并恢复缺失记录。")
            # 完成标志只在完整集合验证之后写入，部分写入仍保留，便于恢复。
            self._write_complete_marker()
            self._remember_vectors(vectors)
            self.vector_db = database
            self.diagnostics["cache_status"] = status

    def search(self, text: str) -> str:
        self.last_evidence = []
        self.diagnostics.pop("last_error", None)
        if self._closed:
            raise RagRetrievalError("RAG 检索器已关闭。")
        if not isinstance(text, str):
            raise TypeError("RAG 检索输入必须是文本。")
        text = text.strip()
        if not text:
            return ""
        candidates: dict[int, int] = {}
        for n, weight in ((2, 1), (3, 2), (4, 3), (5, 4)):
            for ngram in self._char_ngrams(text, n):
                for index in self._ngram_index.get(f"{n}_{ngram}", ()):
                    candidates[index] = candidates.get(index, 0) + weight
        ngram_matches = []
        for index, _ in sorted(candidates.items(), key=lambda pair: (-pair[1], pair[0]))[:30]:
            doc = self.documents[index]
            score = self._ngram_match_score(text, doc.page_content)
            if score > 0.04:
                ngram_matches.append(self._evidence(doc, score, "ngram"))
        ngram_matches.sort(key=lambda item: item["score"], reverse=True)
        try:
            self._ensure_vector_db()
            total = self.vector_db._collection.count()
            if total < 1:
                raise RagRetrievalError("知识向量库为空，混合检索未完成。")
            results = self.vector_db.similarity_search_with_score(text, k=min(total, 10))
            vector_matches = []
            for document, distance in results:
                distance = float(distance)
                if not math.isfinite(distance) or distance < 0:
                    raise RagRetrievalError("向量检索返回了无效距离，混合检索未完成。")
                if distance < self.threshold:
                    vector_matches.append(self._evidence(document, 1.0 - distance, "vector"))
        except (RagRetrievalError, EmbeddingServiceError) as exc:
            self.diagnostics["last_error"] = str(exc)
            self.diagnostics["cache_status"] = "failed"
            raise
        except Exception as exc:
            message = "RAG 向量检索失败，未产生有效混合检索结果；请检查向量依赖、缓存和 Qwen 设置。"
            self.diagnostics["last_error"] = message
            self.diagnostics["cache_status"] = "failed"
            raise RagRetrievalError(message) from exc
        merged = {item["content"]: dict(item) for item in ngram_matches}
        for item in vector_matches:
            if item["content"] in merged:
                match = merged[item["content"]]
                match["score"] = match["score"] * 0.6 + item["score"] * 0.4 + 0.15
                match["source"] = "ngram+vector"
            else:
                merged[item["content"]] = dict(item)
        combined = sorted(merged.values(), key=lambda item: item["score"], reverse=True)
        self.last_evidence = [dict(item) for item in combined if item["score"] > 0.08][:5]
        self.diagnostics.update(
            ngram_matches=len(ngram_matches),
            vector_matches=len(vector_matches),
            returned_rules=len(self.last_evidence),
        )
        return "\n".join(
            f"匹配规则{number}：内容={item['content']}，类型={item['type']}，"
            f"处理规则={item['rule']}，匹配度={item['score']:.4f}({item['source']})"
            for number, item in enumerate(self.last_evidence, 1)
        )

    @staticmethod
    def _evidence(document, score: float, source: str) -> dict:
        return {
            "content": document.page_content,
            "type": document.metadata.get("call_type", "未知"),
            "rule": document.metadata.get("handle_rule", ""),
            "score": score,
            "source": source,
        }

    def run(self, text: str) -> str:
        return self.search(text)

    def close(self):
        self._embeddings.close()
        # Chroma 可能共享底层进程内系统；不能关闭整个系统而影响其他独立评测例。
        self.vector_db = None
        self._closed = True
