"""RAG 的完全离线测试：替身只代替网络/Chroma I/O，不替换融合算法。"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.rag.embeddings import EmbeddingServiceError, QwenEmbeddings
from src.rag.retriever import KnowledgeDocument, RagRetriever


class FakeEmbeddingAPI:
    def __init__(self, dimensions=4, failure_call=None):
        self.calls = []
        self.dimensions = dimensions
        self.failure_call = failure_call

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == self.failure_call:
            raise RuntimeError("private-token-must-not-appear")
        items = []
        for index, text in enumerate(kwargs["input"]):
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            items.append(SimpleNamespace(index=index, embedding=[digest[i] / 255 for i in range(self.dimensions)]))
        return SimpleNamespace(data=list(reversed(items)))


class FakeCollection:
    def __init__(self):
        self.records = {}
        self.upsert_calls = 0
        self.fail_upsert_once_at = None

    def count(self):
        return len(self.records)

    def get(self, ids, include):
        identifiers = [identifier for identifier in reversed(ids) if identifier in self.records]
        result = {"ids": identifiers}
        for field in include:
            result[field] = [self.records[identifier][field] for identifier in identifiers]
        return result

    def upsert(self, ids, documents, metadatas, embeddings):
        self.upsert_calls += 1
        if self.upsert_calls == self.fail_upsert_once_at:
            raise RuntimeError("simulated interrupted write")
        for identifier, document, metadata, embedding in zip(ids, documents, metadatas, embeddings):
            self.records[identifier] = {
                "documents": document,
                "metadatas": metadata,
                "embeddings": list(embedding),
            }


class FakeDatabase:
    def __init__(self, collection, embeddings=None, results=None, error=None):
        self._collection = collection
        self.embeddings = embeddings
        self.results = results
        self.error = error
        self.calls = []

    def similarity_search_with_score(self, text, k):
        self.calls.append((text, k))
        if self.error:
            raise self.error
        if self.embeddings:
            self.embeddings.embed_query(text)
        if self.results is not None:
            return self.results[:k]
        return [
            (KnowledgeDocument(record["documents"], record["metadatas"]), 0.2)
            for record in list(self._collection.records.values())[:k]
        ]


class EmbeddingTests(unittest.TestCase):
    def test_batches_are_at_most_ten_and_response_is_reordered(self):
        api = FakeEmbeddingAPI()
        embeddings = QwenEmbeddings(client=SimpleNamespace(embeddings=api), dimensions=4, batch_size=80)
        texts = [f"文档{index}" for index in range(25)]
        result = embeddings.embed_documents(texts)
        self.assertEqual([len(call["input"]) for call in api.calls], [10, 10, 5])
        self.assertEqual(result[0][0], hashlib.sha256(texts[0].encode()).digest()[0] / 255)
        self.assertEqual(result[24][0], hashlib.sha256(texts[24].encode()).digest()[0] / 255)
        self.assertEqual(api.calls[0]["model"], "text-embedding-v4")
        self.assertEqual(api.calls[0]["encoding_format"], "float")
        self.assertEqual(api.calls[0]["dimensions"], 4)

    def test_constructor_and_empty_documents_do_not_initialize_client(self):
        embeddings = QwenEmbeddings()
        self.assertIsNone(embeddings.client)
        self.assertEqual(embeddings.embed_documents([]), [])
        with self.assertRaisesRegex(EmbeddingServiceError, "API Key"):
            embeddings.embed_query("查询")

    def test_key_is_not_taken_from_ambient_environment(self):
        with patch.dict("os.environ", {"DASHSCOPE_API_KEY": "old-private-value", "QWEN_API_KEY": "old-private-value"}):
            with self.assertRaisesRegex(EmbeddingServiceError, "API Key"):
                QwenEmbeddings().embed_query("查询")

    def test_bad_responses_are_rejected(self):
        cases = [
            ([], "条数"),
            ([SimpleNamespace(index=1, embedding=[0.0] * 4)], "索引"),
            ([SimpleNamespace(index=True, embedding=[0.0] * 4)], "索引"),
            ([SimpleNamespace(index=0, embedding=[0.0] * 3)], "维度"),
            ([SimpleNamespace(index=0, embedding=[float("nan")] * 4)], "NaN/Inf"),
            ([SimpleNamespace(index=0, embedding=[float("inf")] * 4)], "NaN/Inf"),
            ([SimpleNamespace(index=0, embedding=[True] * 4)], "非数值"),
            ([SimpleNamespace(index=0, embedding=["0"] * 4)], "非数值"),
            ([SimpleNamespace(index=0, embedding=None)], "格式"),
        ]
        for data, message in cases:
            with self.subTest(message=message, data=str(data)):
                client = SimpleNamespace(embeddings=SimpleNamespace(create=lambda **kwargs: SimpleNamespace(data=data)))
                with self.assertRaisesRegex(EmbeddingServiceError, message):
                    QwenEmbeddings(client=client, dimensions=4).embed_query("查询")

    def test_duplicate_indexes_are_rejected(self):
        client = SimpleNamespace(embeddings=SimpleNamespace(create=lambda **kwargs: SimpleNamespace(
            data=[SimpleNamespace(index=0, embedding=[0.0] * 4), SimpleNamespace(index=0, embedding=[0.0] * 4)]
        )))
        with self.assertRaisesRegex(EmbeddingServiceError, "索引"):
            QwenEmbeddings(client=client, dimensions=4).embed_documents(["甲", "乙"])

    def test_error_text_does_not_expose_provider_details(self):
        api = FakeEmbeddingAPI(failure_call=1)
        with self.assertRaises(EmbeddingServiceError) as raised:
            QwenEmbeddings(client=SimpleNamespace(embeddings=api), dimensions=4).embed_query("查询")
        self.assertNotIn("private-token", str(raised.exception))

    def test_close_does_not_close_injected_client(self):
        calls = []
        client = SimpleNamespace(close=lambda: calls.append("close"))
        QwenEmbeddings(client=client).close()
        self.assertEqual(calls, [])


class RagTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.knowledge = self.root / "knowledge.txt"
        self.knowledge.write_text(
            "您的外卖已经送到楼下|外卖|询问放置位置\n"
            "请核对同学聚会的时间安排|亲友|记录后转告\n"
            "您涉嫌违法请转款到安全账户|诈骗|拒绝转款\n",
            encoding="utf-8",
        )
        self.config = {
            "rag": {
                "knowledge_path": str(self.knowledge),
                "chroma_persist_dir": str(self.root / "cache"),
                "embedding_dimensions": 4,
            },
            "llm": {"qwen": {"api_key": "offline-placeholder", "base_url": "https://example.test/v1"}},
        }
        self.collections = {}
        with RagRetriever._cache_lock:
            RagRetriever._document_cache.clear()

    def tearDown(self):
        with RagRetriever._cache_lock:
            RagRetriever._document_cache.clear()
        self.temporary.cleanup()

    def fake_factory(self, retriever):
        collection = self.collections.setdefault(str(retriever.cache_dir), FakeCollection())
        return FakeDatabase(collection, retriever._embeddings)

    def patch_factory(self):
        return patch.object(RagRetriever, "_create_vector_db", lambda retriever: self.fake_factory(retriever))

    def client(self, api=None):
        api = api or FakeEmbeddingAPI()
        return SimpleNamespace(_clients={"qwen": {"client": SimpleNamespace(embeddings=api)}}), api

    def injected_database(self, results=None, error=None):
        collection = SimpleNamespace(count=lambda: 12)
        return FakeDatabase(collection, results=results or [], error=error)

    def test_init_is_local_only_and_invalid_rows_are_reported_unchanged(self):
        self.knowledge.write_text("有效规则内容|正常|记录\n缺列\n甲|乙|丙|丁\n内容||处理\n\n", encoding="utf-8")
        before = self.knowledge.read_bytes()
        with patch.object(RagRetriever, "_create_vector_db", side_effect=AssertionError("constructor must stay offline")):
            retriever = RagRetriever(self.config)
        self.assertEqual(retriever.diagnostics["valid_rows"], 1)
        self.assertEqual(retriever.diagnostics["malformed_line_numbers"], [2, 3, 4])
        self.assertEqual(retriever.knowledge_sha256, hashlib.sha256(before).hexdigest())
        self.assertEqual(self.knowledge.read_bytes(), before)
        self.assertFalse(retriever.cache_dir.exists())

    def test_only_explicit_configured_environment_variable_is_read(self):
        self.config["llm"]["qwen"]["api_key"] = ""
        self.config["llm"]["qwen"]["api_key_env"] = "YANXI_RAG_TEST_KEY"
        with patch.dict(os.environ, {"YANXI_RAG_TEST_KEY": "specified-placeholder", "DASHSCOPE_API_KEY": "old-placeholder"}):
            retriever = RagRetriever(self.config)
        self.assertEqual(retriever._embeddings.api_key, "specified-placeholder")
        self.config["llm"]["qwen"].pop("api_key_env")
        with patch.dict(os.environ, {"QWEN_API_KEY": "ambient-placeholder", "DASHSCOPE_API_KEY": "old-placeholder"}):
            retriever = RagRetriever(self.config)
        self.assertEqual(retriever._embeddings.api_key, "")

    def test_all_malformed_rows_fail_clearly(self):
        self.knowledge.write_text("坏行\n甲|乙\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "没有有效规则"):
            RagRetriever(self.config)

    def test_ngram_preserves_original_multi_granularity_weights(self):
        self.assertEqual(RagRetriever._char_ngrams("外卖，已到！", 2), {"外卖", "卖已", "已到"})
        self.assertEqual(RagRetriever._char_ngrams("外", 2), set())
        self.assertAlmostEqual(RagRetriever._ngram_match_score("甲乙丙丁戊", "甲乙丙丁戊"), 1.0)
        self.assertAlmostEqual(RagRetriever._ngram_match_score("甲乙丙", "甲乙丙"), 0.4)

    def test_exact_original_merge_and_output_contract(self):
        doc = KnowledgeDocument("您的外卖已经送到楼下", {"call_type": "外卖", "handle_rule": "询问放置位置"})
        db = self.injected_database([(doc, 0.2)])
        retriever = RagRetriever(self.config, vector_db=db)
        hint = retriever.search(doc.page_content)
        first = retriever.last_evidence[0]
        self.assertEqual(set(first), {"content", "type", "rule", "score", "source"})
        self.assertEqual(first["source"], "ngram+vector")
        self.assertAlmostEqual(first["score"], 1.0 * 0.6 + 0.8 * 0.4 + 0.15)
        self.assertIn("匹配规则1：", hint)
        self.assertIn("处理规则=询问放置位置", hint)
        self.assertEqual(db.calls, [(doc.page_content, 10)])

    def test_vector_only_top_five_and_threshold(self):
        documents = [KnowledgeDocument(f"无词面重合规则{index}", {"call_type": "测试", "handle_rule": "记录"}) for index in range(8)]
        distances = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.89, 0.90]
        retriever = RagRetriever(self.config, vector_db=self.injected_database(list(zip(documents, distances))))
        retriever.search("abcdefghij")
        self.assertEqual(len(retriever.last_evidence), 5)
        self.assertEqual([entry["content"] for entry in retriever.last_evidence], [doc.page_content for doc in documents[:5]])
        self.assertTrue(all(entry["source"] == "vector" for entry in retriever.last_evidence))
        self.assertEqual(retriever.diagnostics["vector_matches"], 7)

    def test_ngram_without_vector_match_still_requires_successful_vector_request(self):
        database = self.injected_database([])
        retriever = RagRetriever(self.config, vector_db=database)
        self.assertTrue(retriever.search("您的外卖已经送到楼下"))
        self.assertEqual(retriever.last_evidence[0]["source"], "ngram")
        self.assertEqual(len(database.calls), 1)

    def test_vector_failure_never_silently_falls_back_to_ngram(self):
        retriever = RagRetriever(self.config, vector_db=self.injected_database(error=RuntimeError("private-token")))
        with self.assertRaisesRegex(RuntimeError, "RAG 向量检索失败") as raised:
            retriever.search("您的外卖已经送到楼下")
        self.assertNotIn("private-token", str(raised.exception))
        self.assertEqual(retriever.last_evidence, [])

    def test_invalid_vector_distance_fails(self):
        doc = KnowledgeDocument("测试", {})
        for distance in [float("nan"), float("inf"), -0.1]:
            retriever = RagRetriever(self.config, vector_db=self.injected_database([(doc, distance)]))
            with self.assertRaisesRegex(RuntimeError, "无效距离"):
                retriever.search("测试")

    def test_empty_text_never_calls_vector_db(self):
        retriever = RagRetriever(self.config, vector_db=self.injected_database(error=AssertionError("unused")))
        self.assertEqual(retriever.search("  "), "")
        self.assertEqual(retriever.last_evidence, [])

    def test_fingerprint_changes_with_knowledge_endpoint_model_and_dimensions(self):
        first = RagRetriever(self.config)
        self.config["llm"]["qwen"]["base_url"] = "https://second.test/v1"
        second = RagRetriever(self.config)
        self.config["rag"]["embedding_model"] = "other-embedding-model"
        third = RagRetriever(self.config)
        self.config["rag"]["embedding_dimensions"] = 8
        fourth = RagRetriever(self.config)
        self.knowledge.write_text("新的规则|正常|记录\n", encoding="utf-8")
        fifth = RagRetriever(self.config)
        self.assertEqual(len({r.fingerprint for r in (first, second, third, fourth, fifth)}), 5)
        self.assertEqual(first.knowledge_sha256, second.knowledge_sha256)
        self.assertNotEqual(first.knowledge_sha256, fifth.knowledge_sha256)

    def test_complete_build_has_marker_and_unchanged_old_cache(self):
        old = Path(self.config["rag"]["chroma_persist_dir"]) / "original-cache"
        old.mkdir(parents=True)
        (old / "keep.txt").write_text("untouched", encoding="utf-8")
        client, api = self.client()
        retriever = RagRetriever(self.config, client)
        with self.patch_factory():
            retriever.search("新的来电查询")
        marker = json.loads(retriever._marker_path.read_text(encoding="utf-8"))
        self.assertTrue(marker["complete"])
        self.assertEqual(marker["document_count"], 3)
        self.assertEqual([len(call["input"]) for call in api.calls], [3, 1])
        self.assertEqual((old / "keep.txt").read_text(encoding="utf-8"), "untouched")

    def test_separate_case_directory_reuses_only_immutable_document_vectors(self):
        client1, api1 = self.client()
        first = RagRetriever(self.config, client1)
        with self.patch_factory():
            first.search("第一例来电")
            self.config["rag"]["chroma_persist_dir"] = str(self.root / "separate-case")
            client2, api2 = self.client()
            second = RagRetriever(self.config, client2)
            second.search("第二例来电")
            second.search("第二例来电")
        self.assertNotEqual(first.cache_dir, second.cache_dir)
        self.assertIsNot(first.vector_db._collection, second.vector_db._collection)
        self.assertIsNot(first._embeddings.client, second._embeddings.client)
        self.assertEqual([call["input"] for call in api2.calls], [["第二例来电"], ["第二例来电"]])
        self.assertEqual([len(call["input"]) for call in api1.calls], [3, 1])
        self.assertEqual(second.diagnostics["document_embedding_cache"], "memory_hit")
        cached = RagRetriever._document_cache[first.fingerprint]
        self.assertIsInstance(cached, tuple)
        self.assertTrue(all(isinstance(vector, tuple) for vector in cached))

    def test_existing_complete_disk_index_avoids_document_api_calls(self):
        first, api1 = self.client()
        with self.patch_factory():
            original = RagRetriever(self.config, first)
            original.search("第一次")
            RagRetriever._document_cache.clear()
            second, api2 = self.client()
            loaded = RagRetriever(self.config, second)
            loaded.search("第二次")
        self.assertEqual([call["input"] for call in api2.calls], [["第二次"]])
        self.assertEqual(loaded.diagnostics["cache_status"], "loaded")
        self.assertEqual(loaded.diagnostics["document_embedding_cache"], "disk_loaded")

    def test_incomplete_write_has_no_marker_or_memory_cache_and_recovers(self):
        self.knowledge.write_text("\n".join(f"独立场景规则{index}|测试|记录" for index in range(40)), encoding="utf-8")
        client, api = self.client()
        first = RagRetriever(self.config, client)
        collection = FakeCollection()
        collection.fail_upsert_once_at = 2
        self.collections[str(first.cache_dir)] = collection
        with self.patch_factory():
            with self.assertRaises(RuntimeError):
                first.search("查询")
            self.assertEqual(collection.count(), 32)
            self.assertFalse(first._marker_path.exists())
            self.assertNotIn(first.fingerprint, RagRetriever._document_cache)
            second = RagRetriever(self.config, client)
            second.search("再次查询")
        self.assertEqual(collection.count(), 40)
        self.assertTrue(second._marker_path.exists())
        self.assertEqual(second.diagnostics["cache_status"], "recovered")

    def test_partial_embedding_response_never_populates_document_cache(self):
        self.knowledge.write_text("\n".join(f"规则内容{index}|测试|记录" for index in range(12)), encoding="utf-8")
        client, _ = self.client(FakeEmbeddingAPI(failure_call=2))
        retriever = RagRetriever(self.config, client)
        with self.patch_factory():
            with self.assertRaises(EmbeddingServiceError):
                retriever.search("查询")
        self.assertFalse(retriever._marker_path.exists())
        self.assertNotIn(retriever.fingerprint, RagRetriever._document_cache)
        self.assertEqual(self.collections[str(retriever.cache_dir)].count(), 0)

    def test_unknown_cache_entries_are_preserved_and_refused(self):
        retriever = RagRetriever(self.config)
        collection = FakeCollection()
        collection.records["foreign"] = {"documents": "保留", "metadatas": {}, "embeddings": [0.0] * 4}
        self.collections[str(retriever.cache_dir)] = collection
        with self.patch_factory():
            with self.assertRaisesRegex(RuntimeError, "旧数据未删除"):
                retriever.search("查询")
        self.assertIn("foreign", collection.records)

    def test_close_makes_retriever_unusable_without_affecting_shared_client(self):
        retriever = RagRetriever(self.config, vector_db=self.injected_database())
        retriever.close()
        with self.assertRaisesRegex(RuntimeError, "已关闭"):
            retriever.search("查询")

    @unittest.skipUnless(importlib.util.find_spec("langchain_chroma"), "optional local Chroma integration")
    def test_real_chroma_io_uses_fake_embedding_service_without_network(self):
        # 原桌面测试会还原 sys.modules；numpy 原生模块被导入后不能再次载入。
        # 使用独立解释器执行真正的 Chroma I/O，不污染/依赖主测试进程状态。
        if os.environ.get("YANXI_RAG_CHROMA_TEST_CHILD") != "1":
            environment = os.environ.copy()
            environment["YANXI_RAG_CHROMA_TEST_CHILD"] = "1"
            environment["ANONYMIZED_TELEMETRY"] = "False"
            result = subprocess.run(
                [sys.executable, "-X", "utf8", "-m", "unittest",
                 "tests.test_rag.RagTests.test_real_chroma_io_uses_fake_embedding_service_without_network"],
                cwd=str(Path(__file__).resolve().parents[1]),
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=45,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return
        client, api = self.client()
        retriever = RagRetriever(self.config, client)
        database = None
        try:
            with patch.dict("os.environ", {"ANONYMIZED_TELEMETRY": "False"}):
                retriever.search("您的外卖已经送到楼下")
                database = retriever.vector_db
                self.assertEqual(database._collection.count(), 3)
                self.assertEqual(retriever.last_evidence[0]["source"], "ngram+vector")
                self.assertEqual(retriever.last_evidence[0]["content"], "您的外卖已经送到楼下")
                self.assertEqual([len(call["input"]) for call in api.calls], [3, 1])
        finally:
            retriever.close()
            # 测试独占这一临时 Chroma 系统；仅在测试中释放 Windows 文件句柄。
            # 生产 close 不这样做，避免终止同进程其他评测实例的 Chroma 系统。
            if database is not None:
                database._client._system.stop()


if __name__ == "__main__":
    unittest.main()
