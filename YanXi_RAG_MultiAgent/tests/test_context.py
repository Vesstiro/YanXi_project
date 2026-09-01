"""Offline regression tests for owner, number and call-history persistence."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.context.stores import CallerStore, HistoryStore, OwnerSettingsStore, StoreDataError, normalize_number


class TemporaryStoreCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)


class NumberTests(unittest.TestCase):
    def test_mobile_country_code_variations_share_one_number(self):
        for number in ("13800138000", "+86 138-0013-8000", "0086 (138) 0013 8000",
                       "86 13800138000", "＋８６ １３８００１３８０００"):
            with self.subTest(number=number):
                self.assertEqual(normalize_number(number), "13800138000")

    def test_valid_international_and_landlines_are_not_merged(self):
        for source, expected in (("+1 (212) 555-0123", "+12125550123"),
                                 ("0044 20 7946 0018", "+442079460018"),
                                 ("(010) 1234-5678", "01012345678"),
                                 ("0571-87654321", "057187654321"),
                                 ("+86 10 12345678", "+861012345678"),
                                 ("95588", "95588"), ("120", "120"), ("88888888", "88888888")):
            with self.subTest(source=source):
                self.assertEqual(normalize_number(source), expected)

    def test_unknown_labels_extensions_and_malformed_numbers_are_rejected(self):
        for number in ("unknown", "未知号码", "匿名", "010-12345678 转 12", "010-12345678-1234", "010-1234567-1",
                       "13800138000x10", "+1 2125550123 ext 99", "1+3800138000", "(01012345678",
                       "0000000", "12", "+1234567890123456", "13800138000;123"):
            with self.subTest(number=number), self.assertRaises(ValueError):
                normalize_number(number)

    def test_only_empty_numbers_mean_anonymous(self):
        self.assertEqual(normalize_number(None), "")
        self.assertEqual(normalize_number("  "), "")
        with self.assertRaises(ValueError):
            normalize_number(13800138000)


class OwnerTests(TemporaryStoreCase):
    def test_defaults_are_local_and_do_not_create_a_file(self):
        store = OwnerSettingsStore(self.root)
        settings = store.load()
        self.assertEqual(settings["presence_mode"], "free")
        self.assertEqual(settings["reason"], "")
        self.assertEqual(settings["duration_minutes"], 0)
        self.assertEqual(settings["expires_at"], 0)
        self.assertFalse(settings["allow_order_changes"])
        self.assertFalse(store.path.exists())

    def test_save_is_persistent_and_returns_a_copy(self):
        store = OwnerSettingsStore(root=self.root)
        saved = store.save({"presence_mode": "busy", "reason": "开会", "duration_minutes": 30,
                            "allowed_dropoff": "指定驿站", "allow_order_changes": True,
                            "callback_note": "请留下回电号码"})
        self.assertAlmostEqual(saved["expires_at"] - saved["updated_at"], 1800)
        saved["reason"] = "意外修改"
        self.assertEqual(OwnerSettingsStore(self.root).load()["reason"], "开会")
        self.assertEqual(store.load()["allowed_dropoff"], "指定驿站")

    def test_expiration_clears_old_presence_and_persists_free(self):
        store = OwnerSettingsStore(self.root)
        with patch("src.context.stores.time.time", return_value=1000):
            store.save({"presence_mode": "driving", "reason": "去机场", "duration_minutes": 1,
                        "allowed_dropoff": "前台"})
        with patch("src.context.stores.time.time", return_value=1060):
            expired = store.load()
        self.assertEqual(expired["presence_mode"], "free")
        self.assertEqual(expired["reason"], "")
        self.assertEqual(expired["duration_minutes"], 0)
        self.assertEqual(expired["expires_at"], 0)
        self.assertEqual(expired["allowed_dropoff"], "前台")
        self.assertEqual(json.loads(store.path.read_text(encoding="utf-8"))["reason"], "")

    def test_free_and_manual_modes_never_leak_old_busy_reason(self):
        store = OwnerSettingsStore(self.root)
        busy = store.save({"presence_mode": "busy", "reason": "学习", "duration_minutes": 0})
        self.assertEqual(busy["expires_at"], 0)
        free = store.save({"presence_mode": "free"})
        self.assertEqual(free["reason"], "")
        self.assertEqual(free["duration_minutes"], 0)

    def test_invalid_preferences_are_rejected_without_replacement(self):
        store = OwnerSettingsStore(self.root)
        store.save({"presence_mode": "busy", "reason": "开会"})
        before = store.path.read_bytes()
        for settings in ({"presence_mode": "holiday"}, {"duration_minutes": -1},
                         {"duration_minutes": 1.5}, {"duration_minutes": True},
                         {"allow_order_changes": "false"}, {"reason": None}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                store.save(settings)
            self.assertEqual(store.path.read_bytes(), before)

    def test_corrupt_settings_are_preserved_and_reported(self):
        store = OwnerSettingsStore(self.root)
        store.path.parent.mkdir(parents=True)
        store.path.write_text('{"presence_mode":', encoding="utf-8")
        before = store.path.read_bytes()
        with self.assertRaisesRegex(StoreDataError, "原文件未被覆盖"):
            store.load()
        with self.assertRaises(StoreDataError):
            store.save({"presence_mode": "free"})
        self.assertEqual(store.path.read_bytes(), before)

    def test_unknown_old_auto_reply_is_not_exposed(self):
        store = OwnerSettingsStore(self.root)
        store.path.parent.mkdir(parents=True)
        store.path.write_text(json.dumps({"presence_mode": "free", "reason": "过去开会",
                                          "auto_reply": "机主还在开会"}), encoding="utf-8")
        loaded = store.load()
        self.assertEqual(loaded["reason"], "")
        self.assertNotIn("auto_reply", loaded)


class CallerTests(TemporaryStoreCase):
    def setUp(self):
        super().setUp()
        self.path = self.root / "profiles.json"
        self.store = CallerStore(self.path)
        self.number = "13800138000"

    def test_reads_and_anonymous_calls_do_not_persist(self):
        self.assertEqual(self.store.get(self.number)["trust_score"], 50)
        self.assertEqual(self.store.get("")["call_count"], 0)
        self.assertEqual(self.store.record_call("", "scam", "high")["trust_score"], 50)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.store.list_profiles(), [])
        with self.assertRaises(ValueError):
            self.store.set_contact("", "匿名")

    def test_normalized_numbers_and_call_id_are_idempotent(self):
        first = self.store.record_call(self.number, "general", "low", call_id="call-1")
        again = CallerStore(self.path).record_call("+86 138 0013 8000", "scam", "high", call_id="call-1")
        self.assertEqual(again, first)
        self.assertEqual(again["call_count"], 1)
        self.assertEqual(again["trust_score"], 51)
        self.assertEqual(len(self.store.list_profiles()), 1)

    def test_benign_type_aliases_are_not_penalized(self):
        for index, category in enumerate(("general", "normal", "express", "express_delivery", "family", "friend", "food_delivery")):
            profile = self.store.record_call(self.number, category, "low", call_id=str(index))
            self.assertGreaterEqual(profile["evidence"][-1]["score_delta"], 0)
        self.assertEqual(profile["history_trust_score"], 57)

    def test_unknown_categories_and_api_failures_never_prove_fraud(self):
        for index, (category, risk) in enumerate((("new_unknown_id", "low"), ("new_unknown_id", "high"),
                                                  ("scam", "unknown"), ("scam", "api_error"),
                                                  ("family", "unavailable"), ("scam", "nonsense"))):
            result = self.store.record_call(self.number, category, risk, call_id=str(index))
            self.assertEqual(result["history_trust_score"], 50)
            self.assertEqual(result["list_status"], "normal")
        self.assertEqual(result["call_count"], 6)

    def test_five_level_agent_risk_labels_map_without_losing_original_evidence(self):
        for index, (category, risk, expected_delta) in enumerate((
                ("general", "1级-安全", 1), ("friend", "2级-低风险", 0),
                ("telemarketing", "3级-中风险", 0), ("scam_risk", "4级-高风险", -15),
                ("scam", "5级-高危", -25))):
            with self.subTest(risk=risk):
                profile = self.store.record_call(self.number, category, risk, call_id=f"risk-{index}")
                self.assertEqual(profile["last_risk_level"], risk)
                self.assertEqual(profile["evidence"][-1]["risk_level"], risk)
                self.assertEqual(profile["evidence"][-1]["score_delta"], expected_delta)

    def test_risky_calls_never_automatically_blacklist(self):
        for index in range(5):
            profile = self.store.record_call(self.number, "scam", "high", call_id=str(index))
        self.assertEqual(profile["history_trust_score"], 0)
        self.assertEqual(profile["list_status"], "normal")
        self.assertIn("身份", profile["trust_description"])

    def test_manual_lists_are_exclusive_and_preserve_behavior_score(self):
        self.store.record_call(self.number, "general", "low", call_id="first")
        white = self.store.set_contact(self.number, "妈妈", list_status="whitelist", notes="人工录入")
        self.assertEqual(white["trust_score"], 100)
        risk = self.store.record_call(self.number, "scam", "high", call_id="second")
        self.assertEqual(risk["list_status"], "whitelist")
        self.assertEqual(risk["trust_score"], 100)
        self.assertEqual(risk["history_trust_score"], 26)
        black = self.store.set_contact(self.number, "待核实", list_status="blacklist")
        self.assertEqual(black["trust_score"], 0)
        self.assertEqual(black["list_status"], "blacklist")
        cleared = self.store.set_contact(self.number)
        self.assertEqual(cleared["trust_score"], 26)
        self.assertEqual(cleared["list_status"], "normal")

    def test_manual_override_is_independent_of_history_and_can_be_cleared(self):
        contact = self.store.set_contact(self.number, "联系人", trust_override=75)
        self.assertEqual(contact["trust_score"], 75)
        called = self.store.record_call(self.number, "general", "low", call_id="1")
        self.assertEqual(called["trust_score"], 75)
        self.assertEqual(called["history_trust_score"], 51)
        self.assertEqual(self.store.set_contact(self.number, trust_override=None)["trust_score"], 51)

    def test_invalid_manual_settings_leave_file_unchanged(self):
        self.store.set_contact(self.number)
        before = self.path.read_bytes()
        for kwargs in ({"trust_override": -1}, {"trust_override": 101},
                       {"trust_override": float("nan")}, {"trust_override": True},
                       {"list_status": "whitelist,blacklist"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.store.set_contact(self.number, **kwargs)
            self.assertEqual(self.path.read_bytes(), before)

    def test_twenty_recent_evidence_items_keep_full_deduplication(self):
        for index in range(25):
            self.store.record_call(self.number, "general", "low", call_id=str(index))
        profile = self.store.record_call(self.number, "scam", "high", call_id="0")
        self.assertEqual(profile["call_count"], 25)
        self.assertEqual(len(profile["evidence"]), 20)
        self.assertEqual(profile["evidence"][0]["call_id"], "5")
        self.assertEqual(len(profile["processed_call_ids"]), 25)

    def test_multiple_instances_share_a_lock_without_lost_updates(self):
        def record(index):
            return CallerStore(self.path).record_call(self.number, "general", "low", call_id=str(index))
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(record, list(range(20)) + list(range(20))))
        self.assertEqual(self.store.get(self.number)["call_count"], 20)
        self.assertEqual(self.store.get(self.number)["trust_score"], 70)

    def test_returned_profile_is_a_deep_copy(self):
        saved = self.store.record_call(self.number, "general", "low", call_id="one")
        saved["evidence"][0]["description"] = "changed"
        saved["processed_call_ids"].clear()
        loaded = self.store.get(self.number)
        self.assertNotEqual(loaded["evidence"][0]["description"], "changed")
        self.assertEqual(loaded["processed_call_ids"], ["one"])

    def test_corrupt_profiles_are_not_silently_replaced(self):
        self.path.write_text("not-json", encoding="utf-8")
        with self.assertRaises(StoreDataError):
            self.store.get(self.number)
        with self.assertRaises(StoreDataError):
            self.store.set_contact(self.number)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "not-json")

    def test_atomic_replacement_failure_keeps_previous_record(self):
        self.store.set_contact(self.number, "原联系人")
        before = self.path.read_bytes()
        with patch("src.context.stores.os.replace", side_effect=PermissionError("test locked file")):
            with self.assertRaises(PermissionError):
                self.store.set_contact(self.number, "新联系人")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.root.glob("*.tmp")), [])


class HistoryTests(TemporaryStoreCase):
    def setUp(self):
        super().setUp()
        self.store = HistoryStore(self.root / "history")

    def test_preserves_complete_result_and_does_not_mutate_input(self):
        result = {"call_id": "call/../1", "caller_number": "13800138000", "presence_mode": "busy",
                  "final_action": "record", "agent_reply": "我会转达",
                  "notification_card": {"title": "来电提醒", "data": {"amount": 12}},
                  "caller_profile": {"trust_score": 51}, "agent_trace": ["RAG", "risk"]}
        path = self.store.append(result)
        self.assertEqual(path.parent, self.store.directory)
        self.assertEqual(path.suffix, ".json")
        self.assertNotIn("created_at", result)
        stored = self.store.recent()[0]
        for key, value in result.items():
            self.assertEqual(stored[key], value)
        self.assertIn("created_at", stored)

    def test_duplicate_id_never_overwrites_previous_evidence(self):
        path = self.store.append({"call_id": "one", "agent_reply": "original"})
        again = self.store.append({"call_id": "one", "agent_reply": "changed"})
        self.assertEqual(path, again)
        self.assertEqual(self.store.recent()[0]["agent_reply"], "original")
        self.assertEqual(len(self.store.recent()), 1)

    def test_generated_ids_are_unique(self):
        first = self.store.append({"agent_reply": "first"})
        second = self.store.append({"agent_reply": "second"})
        self.assertNotEqual(first, second)
        self.assertEqual(len(self.store.recent()), 2)

    def test_recent_sorts_by_time_and_honors_limits(self):
        self.store.append({"call_id": "a", "created_at": 30})
        self.store.append({"call_id": "b", "created_at": 10})
        self.store.append({"call_id": "c", "created_at": 20})
        self.assertEqual([record["call_id"] for record in self.store.recent(2)], ["a", "c"])
        self.assertEqual(self.store.recent(0), [])
        with self.assertRaises(ValueError):
            self.store.recent(-1)

    def test_corrupt_history_is_preserved_and_reported(self):
        path = self.store.append({"call_id": "one"})
        path.write_text("{", encoding="utf-8")
        with self.assertRaises(StoreDataError):
            self.store.recent()
        with self.assertRaises(StoreDataError):
            self.store.append({"call_id": "one"})
        self.assertEqual(path.read_text(encoding="utf-8"), "{")

    def test_non_json_records_never_create_partial_files(self):
        for value in ({"bad": float("nan")}, {"bad": {1, 2}}):
            with self.subTest(value=value), self.assertRaises((ValueError, TypeError)):
                self.store.append(value)
        self.assertFalse(self.store.directory.exists())

    def test_empty_history_read_does_not_create_directory(self):
        self.assertEqual(self.store.recent(), [])
        self.assertFalse(self.store.directory.exists())


if __name__ == "__main__":
    unittest.main()
