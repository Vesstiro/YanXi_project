"""Local GUI smoke test using temporary preferences and fake APIs; never reads a real key."""
import argparse
import tempfile
import time
import tkinter as tk
from pathlib import Path
from unittest.mock import patch

from src.evaluation.app import EvaluationApp
from src.evaluation.models import EvaluationCase
from src.evaluation.settings import EvaluationSettings, EvaluationSettingsStore
from tests.test_evaluation import FakeService, sample_judgment
from src.evaluation.agents import validate_judgment
from src.evaluation.presentation import RESULT_TABS, PROCESS_NOTICE


def assert_visible(widget, root):
    if not widget.winfo_ismapped():
        return
    x = widget.winfo_rootx() - root.winfo_rootx()
    y = widget.winfo_rooty() - root.winfo_rooty()
    assert x >= 0 and y >= 0, (str(widget), x, y)
    assert x + widget.winfo_width() <= root.winfo_width() + 2, (str(widget), "right", x, widget.winfo_width(), root.winfo_width())
    assert y + widget.winfo_height() <= root.winfo_height() + 2, (str(widget), "bottom", y, widget.winfo_height(), root.winfo_height())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hold", action="store_true")
    parser.add_argument("--settings", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="yanxi_eval_ui_") as temporary:
        root = tk.Tk()
        app = EvaluationApp(root, store=EvaluationSettingsStore(temporary), auto_load=False, prompt_settings=False)
        root.title("言犀评测台 QA · 模拟数据 · 不调用API")
        app.settings = EvaluationSettings(api_key="TEST-ONLY-NOT-A-REAL-KEY", stt_model="small")
        app.cases = [EvaluationCase("设备检修 · 协调时间", mode="text", source="generated", reference="您好，我们需要确认设备检修时间。"),
            EvaluationCase("门禁验证码 · 安全边界", mode="text", source="generated", reference="您好，请提供您的门禁验证码。"),
            EvaluationCase("无配套原文 · 待人工核听", mode="audio")]
        app.refresh()
        root.update()
        for geometry in ("1450x960", "1160x760"):
            root.geometry(geometry)
            root.update()
            for widget in app.controls + [app.stop_button, app.progress]:
                assert_visible(widget, root)
            assert app.tree.winfo_height() >= 120, app.tree.winfo_height()
            assert_visible(app.result_notebook, root)
            assert len(app.result_notebook.tabs()) == 7
            # Every tab header must fit, including the last one on the narrow layout.
            visible_tabs = set()
            for x in range(app.result_notebook.winfo_width()):
                try:
                    visible_tabs.add(app.result_notebook.index(f"@{x},10"))
                except tk.TclError:
                    pass
            assert visible_tabs == set(range(7)), (geometry, visible_tabs, app.result_notebook.winfo_width())
        app.tree.selection_set([c.id for c in app.cases[:2]])
        directory = Path(temporary) / "run"
        with patch("src.evaluation.app.EvaluationService", FakeService), \
             patch("src.evaluation.app.new_run_directory", return_value=directory), \
             patch("src.evaluation.app.messagebox.askyesno", return_value=True), \
             patch("src.evaluation.app.messagebox.showerror", side_effect=AssertionError):
            # Adapt the test service's constructor to the production signature.
            with patch("src.evaluation.app.EvaluationService", side_effect=lambda s, d: FakeService(d, s)):
                app.start("selected")
                deadline = time.monotonic() + 15
                while app.busy and time.monotonic() < deadline:
                    root.update()
                    time.sleep(0.02)
                assert not app.busy, "worker did not finish"
        assert (directory / "report.html").exists()
        assert app.cases[0].status == "已评审"
        app.cases[1].judgment = validate_judgment(sample_judgment(2, "fail"))
        app.cases[2].status = "已评审"
        app.cases[2].judgment = validate_judgment(sample_judgment(), has_reference=False)
        app.filter.set("不通过")
        app.refresh()
        assert len(app.tree.get_children()) == 1
        app.filter.set("全部")
        app.refresh()
        app.tree.selection_set(app.cases[1].id)
        app.cases[1].output = {"agent_reply": "这是管家直接回复", "final_action": "record",
            "agent_trace": [{"step": "场景分析", "content": "这里只是记录的判断依据"}]}
        app.cases[1].judgment["improved_reply"] = "这是评测AI建议的另一个回复"
        app.show_selected()
        assert app.result_texts["reply"].get("1.0", "end-1c") == "这是管家直接回复"
        assert "不通过" in app.result_texts["evaluation"].get("1.0", "end")
        assert "这是评测AI建议的另一个回复" not in app.result_texts["reply"].get("1.0", "end")
        assert PROCESS_NOTICE in app.result_texts["process"].get("1.0", "end")
        for key in RESULT_TABS:
            app.result_notebook.select(app.result_pages[key])
            root.update()
            assert app.result_texts[key].winfo_ismapped(), key
            assert app.result_texts[key].cget("state") == "disabled"
        # Copy only the visible page, without touching the user's real clipboard.
        app.result_notebook.select(app.result_pages["reply"])
        with patch.object(root, "clipboard_clear"), patch.object(root, "clipboard_append") as copied:
            app.copy_result()
            copied.assert_called_once_with("这是管家直接回复")
        # Refreshes preserve the chosen tab; filtering away selection clears every page.
        app.result_notebook.select(app.result_pages["evidence"])
        app.refresh()
        assert app.result_notebook.select() == app.result_pages["evidence"]
        app.filter.set("通过")
        app.refresh()
        assert app._detail_case_id is None
        for widget in app.result_texts.values():
            assert "这是管家直接回复" not in widget.get("1.0", "end")
        assert app.copy_result_button.instate(["disabled"])
        app.filter.set("全部")
        app.refresh()
        app.tree.selection_set(app.cases[1].id)
        app.show_selected()
        app.remove_selected()
        assert app._detail_case_id is None
        app.open_settings()
        root.update()
        for widget in app.dialog.controls:
            assert_visible(widget, app.dialog)
        assert app.dialog.snapshot().api_key == app.settings.api_key
        app.dialog.close()
        root.geometry("1450x960")
        root.update()
        print("UI_CHECK_OK: seven tabs, exact reply, separate review, evidence, trace, copy, filter/removal clearing, fake batch, report, settings", flush=True)
        if args.settings:
            app.open_settings()
        if args.hold:
            # Any manually clicked API-test button in this QA window remains offline.
            with patch("src.evaluation.app.test_connections", return_value=None):
                root.mainloop()
        else:
            app.close()


if __name__ == "__main__":
    main()
