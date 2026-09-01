import copy
import json
import unittest

from src.evaluation.models import EvaluationCase
from src.evaluation.presentation import PROCESS_NOTICE, RESULT_TABS, result_views
from src.evaluation.agents import validate_judgment
from tests.test_evaluation import sample_judgment

class ResultPresentationTests(unittest.TestCase):
    def example(self):
        judgment = validate_judgment(sample_judgment())
        judgment.update(summary="评测AI独立评价", expected_behavior="应该记录事实", improved_reply="评测AI建议改写")
        return EvaluationCase("模拟案例", status="已评审", reference="配套参考原文", transcript="实际识别文本",
            output={"agent_reply": "管家真正说的话", "final_action": "record",
                    "agent_trace": [{"step": "风险Agent", "content": "本阶段依据", "data": {"risk_level": "1级-安全"}}],
                    "rag_hint": "命中参考规则", "notification_card": {"title": "来电摘要", "body": "通知内容"}},
            judgment=judgment)

    def test_seven_views_and_default_reply_order(self):
        self.assertEqual(len(RESULT_TABS), 7)
        self.assertEqual(next(iter(RESULT_TABS)), "reply")
        self.assertEqual(set(result_views(self.example())), set(RESULT_TABS))

    def test_actual_reply_is_not_reviewer_suggestion(self):
        views = result_views(self.example())
        self.assertEqual(views["reply"], "管家真正说的话")
        self.assertNotIn("评测AI建议改写", views["reply"])
        self.assertIn("评测AI建议改写", views["evaluation"])
        self.assertIn("不是管家原回复", views["evaluation"])

    def test_reply_whitespace_and_multiline_preserved(self):
        case = self.example()
        case.output["agent_reply"] = "  您好。\n已记录。  "
        self.assertEqual(result_views(case)["reply"], case.output["agent_reply"])

    def test_missing_reply_does_not_invent_one(self):
        case = self.example()
        case.output.pop("agent_reply")
        self.assertIn("没有保存", result_views(case)["reply"])
        self.assertNotIn(case.judgment["improved_reply"], result_views(case)["reply"])
        case.output["final_action"] = "reject"
        self.assertIn("没有口头回复", result_views(case)["reply"])

    def test_legacy_final_reply_and_missing_trace_supported(self):
        case = self.example()
        case.output = {"final_reply": "旧版原回复", "final_action": "record"}
        views = result_views(case)
        self.assertEqual(views["reply"], "旧版原回复")
        self.assertIn("不会", views["process"])
        self.assertIn("编造思考过程", views["process"])

    def test_evidence_separate_from_conclusion(self):
        views = result_views(self.example())
        for label in ("场景匹配", "处理正确性", "回复恰当性", "安全与权限"):
            self.assertIn(label, views["evidence"])
        self.assertIn("引用证据", views["evidence"])
        self.assertIn("判断原因", views["evidence"])
        self.assertIn("ASR", views["evidence"])
        self.assertIn("模型原判", views["evidence"])
        self.assertIn("评测AI独立评价", views["evaluation"])

    def test_process_shows_recorded_trace_not_judge(self):
        views = result_views(self.example())
        self.assertIn(PROCESS_NOTICE, views["process"])
        self.assertIn("本阶段依据", views["process"])
        self.assertIn("风险等级", views["process"])
        self.assertIn("命中参考规则", views["process"])
        self.assertNotIn("评测AI建议改写", views["process"])

    def test_partial_process_and_raw_trace_entries_supported(self):
        case = self.example()
        case.output.pop("agent_trace")
        case.output["scene_analysis"] = {"scene_category": "配送", "needs_owner_decision": False}
        self.assertIn("场景类别：配送", result_views(case)["process"])
        case.output["agent_trace"] = ["旧版文字步骤"]
        self.assertIn("旧版文字步骤", result_views(case)["process"])

    def test_input_is_separate(self):
        views = result_views(self.example())
        self.assertIn("配套参考原文", views["input"])
        self.assertIn("实际识别文本", views["input"])
        self.assertNotIn("配套参考原文", views["reply"])

    def test_errors_warnings_and_generation_are_in_runtime(self):
        case = self.example()
        case.status, case.error, case.error_stage = "错误", "模拟API失败", "独立评审"
        case.warnings = ["需人工核听"]
        case.output["save_warning"] = "本地保存失败"
        case.novelty = {"reason": "新场景理由"}
        case.expected_behavior = "生成器建议"
        case.evaluation_config = {"sut_model": "model-example"}
        views = result_views(case)
        for text in ("模拟API失败", "独立评审", "需人工核听", "本地保存失败", "新场景理由", "生成器建议", "model-example"):
            self.assertIn(text, views["runtime"])
        self.assertIn("不代表正常通过", views["evaluation"])

    def test_pending_and_partial_case_no_crash_or_false_verdict(self):
        case = EvaluationCase("尚未运行")
        views = result_views(case)
        self.assertIn("尚未", views["reply"])
        self.assertIn("尚无", views["evaluation"])
        self.assertIn("尚无", views["evidence"])
        self.assertIn("尚未", views["process"])
        self.assertNotIn("最终判定：通过", views["evaluation"])
        case.error = "识别失败"
        self.assertIn("运行详情", result_views(case)["reply"])

    def test_raw_data_is_complete_and_rendering_does_not_mutate(self):
        case = self.example()
        before = copy.deepcopy(case.export())
        views = result_views(case)
        self.assertEqual(json.loads(views["raw"]), before)
        self.assertEqual(case.export(), before)

if __name__ == "__main__":
    unittest.main()
