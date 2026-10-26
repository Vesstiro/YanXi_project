"""Privacy/behaviour regression: no real API calls, microphones, or user data."""
import json
import os
import socket
import tempfile
import unittest
from copy import deepcopy
from dataclasses import asdict
from datetime import date
from pathlib import Path
from unittest.mock import patch

from src.context.stores import CallerStore
from src.verification.core import (QUESTION_SPECS, VerificationController, VerificationError,
    VerificationSession, VerificationSummary, VerificationVault, default_profile, validate_profile)


def reference(count=7):
    raw = default_profile()
    raw["question_count"] = count
    raw["valid_until"] = "2099-12-31"
    for index, (key, *_rest) in enumerate(QUESTION_SPECS):
        raw["fields"][key] = {"enabled": True, "answers": [f"测试资料甲{index}", f"允许别名{index}"]}
    return raw


def answer_all(session, *, wrong=0, skipped=0):
    index = 0
    answers = {spec[2]: f"测试资料甲{i}" for i, spec in enumerate(QUESTION_SPECS)}
    while not session.done:
        answer = "不匹配的内容" if index < wrong else "" if index < wrong + skipped else answers[session.current_prompt]
        session.submit(answer)
        index += 1
    return session.summary()


class ProfileTests(unittest.TestCase):
    def test_counts_and_insufficient_authorized_answers(self):
        for count in (True, 4, 0, "3"):
            raw = reference()
            raw["question_count"] = count
            with self.assertRaises(VerificationError):
                validate_profile(raw)
        raw = reference()
        raw["fields"]["name"]["enabled"] = False
        with self.assertRaises(VerificationError):
            validate_profile(raw)

    def test_revoking_authorization_removes_answer(self):
        raw = reference(3)
        raw["fields"]["name"]["enabled"] = False
        self.assertEqual(validate_profile(raw)["fields"]["name"]["answers"], [])

    def test_validation_errors_do_not_contain_private_values(self):
        raw = reference()
        secret = "private-value-" * 100
        raw["fields"]["name"]["answers"] = [secret]
        with self.assertRaises(VerificationError) as error:
            validate_profile(raw)
        self.assertNotIn(secret, str(error.exception))

    def test_reject_invalid_dates_unknown_answers_and_duplicate_question(self):
        raw = reference()
        for value in ("2026-02-30", "2026-1-1", "明天"):
            raw["valid_until"] = value
            with self.assertRaises(VerificationError):
                validate_profile(raw)
        raw = reference()
        raw["fields"]["name"]["answers"] = ["不知道"]
        with self.assertRaises(VerificationError):
            validate_profile(raw)
        raw = reference()
        raw["custom_question"] = QUESTION_SPECS[0][2]
        with self.assertRaises(VerificationError):
            validate_profile(raw)


class SessionTests(unittest.TestCase):
    def test_exact_question_counts_and_no_per_answer_feedback(self):
        for count in (3, 5, 7):
            with self.subTest(count=count):
                session = VerificationSession(reference(count), "工作业务")
                prompts = []
                while not session.done:
                    prompts.append(session.current_prompt)
                    self.assertIsNone(session.submit("不知道"))
                self.assertEqual(len(prompts), count)
                self.assertEqual(len(set(prompts)), count)
                self.assertEqual(session.summary().unknown, count)
                self.assertEqual(session.current_prompt, "")
                self.assertEqual(session._questions, [])

    def test_mixed_selection_includes_current_matter(self):
        session = VerificationSession(reference(3), "工作业务")
        self.assertTrue(any(q.key in {"matter", "agency", "appointment", "custom"} for q in session._questions))

    def test_aliases_unicode_spacing_punctuation_but_no_substring_matching(self):
        session = VerificationSession(reference(3), "其他事项")
        for index in range(3):
            key = session._questions[index].key
            original_index = next(i for i, spec in enumerate(QUESTION_SPECS) if spec[0] == key)
            session.submit(f" 允许 别名{original_index}！ ")
        self.assertEqual(session.summary().matched, 3)
        session = VerificationSession(reference(3), "其他事项")
        session.submit("不是" + session._questions[0].aliases[0])
        self.assertEqual(session.finish().mismatched, 1)

    def test_five_questions_denominator_and_unknown_coverage(self):
        result = answer_all(VerificationSession(reference(5), "工作业务"), wrong=1, skipped=1)
        self.assertEqual((result.matched, result.mismatched, result.unknown, result.answered), (3, 1, 1, 5))
        self.assertEqual(result.accuracy, .75)
        self.assertEqual(result.coverage, .8)
        self.assertIn("75%", result.owner_text())
        self.assertIn("80%", result.owner_text())

    def test_zero_known_answers_is_not_zero_percent_or_fraud(self):
        result = answer_all(VerificationSession(reference(3), "工作业务"), skipped=3)
        self.assertIsNone(result.accuracy)
        self.assertIn("暂无法计算", result.owner_text())
        self.assertIn("不等于欺骗", result.recommendation)

    def test_incorrect_category_and_expired_matter_never_pad_questions(self):
        session = VerificationSession(reference(7), "日常联系")
        self.assertEqual(session.summary().status, "unavailable")
        raw = reference(5)
        raw["valid_until"] = "2026-10-25"
        session = VerificationSession(raw, "工作业务", today=date(2026, 10, 26))
        self.assertEqual(session.summary().status, "unavailable")
        raw["question_count"] = 3
        session = VerificationSession(raw, "工作业务", today=date(2026, 10, 26))
        self.assertEqual({q.key for q in session._questions}, {"name", "organization", "department"})

    def test_expiry_includes_last_day(self):
        raw = reference(7)
        raw["valid_until"] = "2026-10-26"
        self.assertFalse(VerificationSession(raw, "工作业务", today=date(2026, 10, 26)).done)

    def test_cancellation_and_timeout_keep_denominator_and_clear_references(self):
        current = [0.0]
        session = VerificationSession(reference(5), "工作业务", clock=lambda: current[0], timeout=10)
        with self.assertRaises(VerificationError):
            session.summary()
        session.submit("无法回答")
        current[0] = 10
        self.assertTrue(session.done)
        self.assertEqual(session.summary().status, "timeout")
        self.assertEqual(session.summary().unknown, 5)
        self.assertEqual(session._questions, [])
        with self.assertRaises(VerificationError):
            session.submit("不能在结束后提交")
        second = VerificationSession(reference(5), "工作业务")
        result = second.finish()
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(result.question_count, 5)

    def test_reference_is_frozen_and_raw_submissions_not_retained(self):
        raw = reference(3)
        session = VerificationSession(raw, "其他事项")
        prompt = session.current_prompt
        raw["fields"][session._questions[0].key]["answers"] = ["篡改后的资料"]
        answers = {spec[2]: f"测试资料甲{i}" for i, spec in enumerate(QUESTION_SPECS)}
        session.submit(answers[prompt])
        private_answer = "caller-private-secret-123"
        session.submit(private_answer)
        result = session.finish()
        serialized = json.dumps(asdict(result), ensure_ascii=False)
        self.assertEqual(result.matched, 1)
        self.assertNotIn(private_answer, repr(vars(session)))
        for value in (private_answer, "测试资料甲", "允许别名", "篡改后的资料"):
            self.assertNotIn(value, serialized + result.owner_text() + repr(session))

    def test_local_flow_does_not_use_network(self):
        with patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden")):
            result = answer_all(VerificationSession(reference(7), "工作业务"))
        self.assertEqual(result.matched, 7)


@unittest.skipUnless(os.name == "nt", "Windows DPAPI integration")
class VaultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="yanxi_verification_")
        self.addCleanup(self.temp.cleanup)
        self.vault = VerificationVault(self.temp.name)

    def test_real_dpapi_round_trip_ciphertext_and_revocation(self):
        self.assertIsNone(self.vault.load())
        raw = reference(5)
        self.vault.save(raw)
        self.assertEqual(self.vault.load(), validate_profile(raw))
        content = self.vault.path.read_text(encoding="utf-8")
        for secret in ("测试资料甲", "允许别名", raw["custom_question"]):
            self.assertNotIn(secret, content)
        raw["fields"]["name"]["enabled"] = False
        self.vault.save(raw)
        self.assertEqual(self.vault.load()["fields"]["name"]["answers"], [])
        self.vault.delete()
        self.assertIsNone(self.vault.load())

    def test_corrupt_ciphertext_fails_closed_and_is_not_overwritten(self):
        self.vault.save(reference())
        envelope = json.loads(self.vault.path.read_text(encoding="utf-8"))
        envelope["sealed"] = "bm90LWEtZH B hcGktYmxvYg=="
        self.vault.path.write_text(json.dumps(envelope), encoding="utf-8")
        before = self.vault.path.read_bytes()
        with self.assertRaises(VerificationError):
            self.vault.load()
        with self.assertRaises(VerificationError):
            self.vault.save(reference())
        self.assertEqual(self.vault.path.read_bytes(), before)

    def test_invalid_dpapi_bytes_have_no_plaintext_fallback(self):
        self.vault.path.parent.mkdir(parents=True)
        self.vault.path.write_text(json.dumps({"version": 1, "protection": "windows-user-dpapi", "sealed": "YWJj"}), encoding="utf-8")
        with self.assertRaises(VerificationError):
            self.vault.load()


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="yanxi_verify_controller_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.callers = CallerStore(self.root / "data/profiles/caller_profiles.json")
        self.vault = type("MemoryVault", (), {"load": lambda _self: deepcopy(reference(3))})()
        self.controller = VerificationController(self.root, vault=self.vault, callers=self.callers)

    def test_only_explicit_contacts_and_whitelist_skip(self):
        number = "13800001111"
        self.callers.set_contact(number, trust_override=100)
        self.assertIsInstance(self.controller.start(number, "工作业务"), VerificationSession)
        self.callers.set_contact(number, display_name="手动联系人")
        self.assertEqual(self.controller.start("+86 138-0000-1111", "工作业务").status, "known")
        self.callers.set_contact(number, list_status="whitelist")
        self.assertEqual(self.controller.start(number, "工作业务").status, "known")
        self.callers.set_contact(number, list_status="blacklist")
        self.assertEqual(self.controller.start(number, "工作业务").status, "blacklist")

    def test_hidden_number_is_stranger_no_contact_writes_history_or_export(self):
        with patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden")):
            session = self.controller.start("", "工作业务")
            answer_all(session)
        self.assertFalse((self.root / "data").exists())

    def test_no_vault_known_callers_still_skip_and_invalid_category_rejected(self):
        self.vault.load = lambda: None
        with self.assertRaises(VerificationError):
            self.controller.start("", "工作业务")
        self.callers.set_contact("13800001111", display_name="已登记")
        self.assertEqual(self.controller.start("13800001111", "工作业务").status, "known")
        with self.assertRaises(VerificationError):
            self.controller.start("", "unknown-category")


if __name__ == "__main__":
    unittest.main()
