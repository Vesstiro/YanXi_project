"""Offline regressions for the daily GUI/context adapter; no network or devices."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.desktop.app import DesktopApp
from src.desktop.context_dialog import ContextController, owner_summary
from src.desktop.service import DesktopService, Job, readable_result, reasoning_details
from src.desktop.settings import DesktopSettings, build_config


class ConfigIsolationTests(unittest.TestCase):
    def test_default_config_does_not_read_daily_owner(self):
        with tempfile.TemporaryDirectory() as directory, patch("src.context.stores.OwnerSettingsStore") as store:
            store.side_effect = AssertionError("Evaluation must not load the daily owner")
            config = build_config(DesktopSettings(api_key="offline-test-key"), root=directory)
        self.assertNotIn("owner", config)

    def test_desktop_explicitly_loads_daily_owner_and_resolves_paths(self):
        with tempfile.TemporaryDirectory() as directory, patch("src.context.stores.OwnerSettingsStore") as store:
            store.return_value.load.return_value = {"presence_mode": "driving", "allowed_dropoff": ""}
            config = build_config(DesktopSettings(api_key="offline-test-key"), include_owner=True, root=directory)
            store.assert_called_once_with(root=Path(directory))
            self.assertEqual(config["owner"]["presence_mode"], "driving")
            self.assertEqual(Path(config["rag"]["knowledge_path"]), Path(directory) / "assets" / "knowledge.txt")
            for section, key in (("rag", "chroma_persist_dir"), ("caller_profile", "persist_path"),
                                 ("call_log", "persist_dir")):
                self.assertTrue(Path(config[section][key]).is_relative_to(Path(directory)))

    def test_evaluator_does_not_inherit_owner_authorizations(self):
        from src.evaluation.settings import EvaluationSettings, isolated_config
        with tempfile.TemporaryDirectory() as directory, patch("src.context.stores.OwnerSettingsStore") as store:
            store.side_effect = AssertionError("Evaluation owner leak")
            config = isolated_config(EvaluationSettings(api_key="offline-test-key"), directory)
            self.assertNotIn("owner", config)
            self.assertEqual(Path(config["caller_profile"]["persist_path"]), Path(directory) / "profiles.json")
            self.assertEqual(Path(config["call_log"]["persist_dir"]), Path(directory) / "call_logs")
            self.assertEqual(Path(config["rag"]["chroma_persist_dir"]), Path(directory) / "chroma")


class ContextControllerTests(unittest.TestCase):
    def controller(self, idle=True):
        owner, callers, history, changed = (MagicMock() for _ in range(4))
        owner.save.side_effect = lambda value: value
        controller = ContextController(owner_store=owner, caller_store=callers, history_store=history,
            can_edit=lambda: idle, changed=changed)
        return controller, owner, callers, changed

    def test_busy_state_blocks_owner_and_contact_mutations(self):
        controller, owner, callers, changed = self.controller(idle=False)
        with self.assertRaises(RuntimeError):
            controller.save_owner({"presence_mode": "busy"})
        with self.assertRaises(RuntimeError):
            controller.save_contact("13800000000", list_status="blacklist")
        owner.save.assert_not_called()
        callers.set_contact.assert_not_called()
        changed.assert_not_called()

    def test_save_owner_preserves_explicit_authorizations(self):
        controller, owner, callers, changed = self.controller()
        value = {"presence_mode": "dnd", "reason": "会议", "duration_minutes": 30,
            "allowed_dropoff": "前台", "allow_order_changes": False, "callback_note": ""}
        self.assertEqual(controller.save_owner(value), value)
        owner.save.assert_called_once_with(value)
        changed.assert_called_once_with()
        callers.set_contact.assert_not_called()

    def test_contact_list_status_is_single_exclusive_choice(self):
        controller, _owner, callers, changed = self.controller()
        controller.save_contact("13800000000", "快递员", "blacklist", "25", "主动登记")
        callers.set_contact.assert_called_once_with("13800000000", display_name="快递员", trust_override=25.0,
            list_status="blacklist", notes="主动登记")
        changed.assert_called_once_with()
        with self.assertRaises(ValueError):
            controller.save_contact("13800000000", list_status="blacklist,whitelist")

    def test_blank_override_uses_history_score(self):
        controller, _owner, callers, _changed = self.controller()
        controller.save_contact("13800000000", trust_override="  ")
        self.assertIsNone(callers.set_contact.call_args.kwargs["trust_override"])

    def test_nonfinite_or_out_of_range_scores_are_rejected(self):
        for value in ("nan", "inf", "-1", "101", "abc"):
            with self.subTest(value=value):
                controller, _owner, callers, changed = self.controller()
                with self.assertRaises(ValueError):
                    controller.save_contact("13800000000", trust_override=value)
                callers.set_contact.assert_not_called()
                changed.assert_not_called()

    def test_failed_storage_does_not_recreate_facade(self):
        controller, owner, _callers, changed = self.controller()
        owner.save.side_effect = OSError("offline disk failure")
        with self.assertRaises(OSError):
            controller.save_owner({"presence_mode": "free"})
        changed.assert_not_called()


class ServiceContextTests(unittest.TestCase):
    def test_every_job_refreshes_owner_even_if_facade_is_reused(self):
        settings = DesktopSettings(api_key="offline-test-key")
        with tempfile.TemporaryDirectory() as directory:
            service = DesktopService(settings, root=directory)
            facade = SimpleNamespace(llm_client=SimpleNamespace(last_error=None), set_owner_context=MagicMock(),
                run=MagicMock(return_value={"agent_reply": "请留言", "final_action": "record"}))
            service.orchestrator = facade
            with patch("src.context.stores.OwnerSettingsStore") as store:
                store.return_value.load.side_effect = [{"presence_mode": "busy"}, {"presence_mode": "free"}]
                service.process(Job("text-1", kind="text", text="请帮我留言", caller_number="13800000000"), lambda _: None)
                service.process(Job("text-2", kind="text", text="现在能接电话吗"), lambda _: None)
            self.assertEqual([call.args[0]["presence_mode"] for call in facade.set_owner_context.call_args_list], ["busy", "free"])
            self.assertEqual(facade.run.call_args_list[0].kwargs["caller_number"], "13800000000")

    def test_first_job_explicitly_requests_owner_config(self):
        service = DesktopService(DesktopSettings(api_key="offline-test-key"))
        facade = SimpleNamespace(llm_client=SimpleNamespace(last_error=None), set_owner_context=MagicMock(),
            run=MagicMock(return_value={"final_action": "record", "agent_reply": "请留言"}))
        owner = {"presence_mode": "driving"}
        constructor = MagicMock(return_value=facade)
        module = SimpleNamespace(CallOrchestrator=constructor)
        with patch.dict("sys.modules", {"src.orchestration.orchestrator": module}), \
                patch("src.desktop.service.build_config", return_value={"owner": owner}) as config:
            service.process(Job("text", kind="text", text="请回电"), lambda _: None)
        self.assertTrue(config.call_args.kwargs["include_owner"])
        facade.set_owner_context.assert_called_once_with(owner)

    def test_close_prefers_new_facade_lifecycle_and_is_idempotent(self):
        service = DesktopService(DesktopSettings(api_key="offline-test-key"))
        close = MagicMock()
        service.orchestrator = SimpleNamespace(close=close)
        service.close()
        service.close()
        close.assert_called_once_with()
        self.assertIsNone(service.orchestrator)


class GuiContextTests(unittest.TestCase):
    def test_state_summary_explains_unauthorized_defaults(self):
        text = owner_summary({"presence_mode": "driving", "reason": "正在驾车", "allowed_dropoff": "", "allow_order_changes": False})
        self.assertIn("驾车", text)
        self.assertIn("放置授权：未设置", text)
        self.assertIn("等价改单：未授权", text)

    def test_context_change_closes_only_idle_service(self):
        app = SimpleNamespace(idle=lambda: True, service=SimpleNamespace(close=MagicMock()),
            update_owner_summary=MagicMock(), status=SimpleNamespace(configure=MagicMock()))
        DesktopApp.context_changed(app)
        app.service.close.assert_called_once_with()
        app.update_owner_summary.assert_called_once_with()
        app.idle = lambda: False
        with self.assertRaises(RuntimeError):
            DesktopApp.context_changed(app)
        app.service.close.assert_called_once_with()

    def test_active_job_cannot_open_mutating_context_dialog(self):
        app = SimpleNamespace(idle=lambda: False)
        with patch("src.desktop.context_dialog.ContextDialog") as dialog:
            DesktopApp.open_context(app)
        dialog.assert_not_called()

    def test_reasoning_tab_contains_retrieval_and_structured_agent_outputs(self):
        result = {"rag_hint": "配送案例知识", "rag_evidence": [{"content": "先核实是否允许放置"}],
            "knowledge_sha256": "offline-sha", "agent_trace": [{"step": "risk", "content": "低风险"}],
            "business_result": {"reply": "请确认放置位置"}}
        text = reasoning_details(result)
        for expected in ("配送案例知识", "先核实", "offline-sha", "业务 Agent", "低风险"):
            self.assertIn(expected, text)

    def test_readable_output_does_not_call_trust_identity_verification(self):
        text = readable_result({"call_type_name": "普通来电", "caller_number": "13800000000",
            "caller_profile": {"trust_score": 100}, "presence_mode": "busy"})
        self.assertIn("100 / 100", text)
        self.assertIn("不代表来电者身份已验证", text)
        self.assertIn("忙碌", text)


if __name__ == "__main__":
    unittest.main()
