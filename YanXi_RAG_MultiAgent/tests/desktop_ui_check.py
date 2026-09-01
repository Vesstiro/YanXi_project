"""Local Tk layout/interaction smoke test with fake processing and no microphone/API."""
import argparse
import tempfile
import time
import tkinter as tk
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.desktop.app import DesktopApp, SettingsDialog
from src.desktop.service import Job
from src.desktop.settings import DesktopSettings, SettingsStore


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hold", action="store_true", help="Keep the separate QA window open for visual inspection")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="yanxi_ui_check_") as directory:
        store = SettingsStore(directory)
        settings = DesktopSettings(api_key="sk-ui-placeholder-not-real")
        store.load = lambda: settings
        def process(job, progress, allow_download):
            job.transcript = job.text or "我是美团外卖，你的餐放在门口了。"
            progress("测试后台任务")
            if "bad" in job.source:
                raise ValueError("测试文件解码失败")
            return {"type_id": "food_delivery", "call_type_name": "外卖配送", "confidence": 0.92,
                "classify_method": "keyword", "final_action": "summary_card", "agent_reply": "好的，请放在门口，谢谢。",
                "notification_card": {"title": "外卖已送达", "body": "美团外卖已放在门口。"}}
        fake = SimpleNamespace(settings=settings, transcriber=SimpleNamespace(is_cached=lambda: True),
            process=process, close=lambda: None)
        root = tk.Tk()
        app = DesktopApp(root, store=store, service=fake, prompt_setup=False)
        root.title("言犀 · 界面验收（测试数据，无真实 API）")
        root.update()
        for size in ("1320x840", "1120x740"):
            root.geometry(size)
            root.update()
            bottom = root.winfo_rooty() + root.winfo_height()
            for name, widget in (("status", app.status), ("progress", app.progress),
                                 ("start", app.start_button), ("speak", app.speak_button)):
                assert widget.winfo_ismapped(), f"{size}: {name} not mapped"
                assert widget.winfo_rooty() + widget.winfo_height() <= bottom, f"{size}: {name} clipped"
        app.text_input.insert("1.0", "我是美团外卖，你的餐放在门口了。")
        app.add_text()
        assert len(app.jobs) == 1
        app.add_job(Job("bad.wav"))
        app.add_job(Job("sample.wav"))
        app.start_batch()
        deadline = time.monotonic() + 10
        while app.processing and time.monotonic() < deadline:
            root.update()
            time.sleep(0.02)
        assert [j.status for j in app.jobs.values()] == ["完成", "失败", "完成"]
        assert "外卖配送" in app.outputs["result"].get("1.0", "end")
        target = Path(directory) / "export.json"
        with patch("src.desktop.app.filedialog.asksaveasfilename", return_value=str(target)):
            app.export_results()
        assert target.is_file()
        assert "sk-ui-placeholder" not in target.read_text(encoding="utf-8")
        dialog = SettingsDialog(app)
        root.update()
        bottom = dialog.winfo_rooty() + dialog.winfo_height()
        assert dialog.save_button.winfo_rooty() + dialog.save_button.winfo_height() <= bottom, "Settings save clipped"
        dialog.cancel()
        root.update()
        root.geometry("1320x840")
        print("GUI_CHECK_OK: responsive layout, text input, batch success/failure, export, settings dialog", flush=True)
        if args.hold:
            root.mainloop()
        else:
            app.close()


if __name__ == "__main__":
    main()
