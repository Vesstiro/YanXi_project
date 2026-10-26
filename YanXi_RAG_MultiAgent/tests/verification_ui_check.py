"""Isolated Tk QA with fictional data, no API key or real call/recording.

Run: python -m tests.verification_ui_check [--hold owner|caller|summary]
"""
import argparse
import socket
import tempfile
import tkinter as tk
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.context.stores import CallerStore
from src.desktop.app import DesktopApp
from src.desktop.settings import DesktopSettings, SettingsStore
from src.desktop.verification_dialog import PreRingDialog, RingSummaryDialog, VerificationOwnerDialog
from src.verification.core import CATEGORIES, QUESTION_SPECS, VerificationVault


def rings(root):
    return [w for w in root.winfo_children() if isinstance(w, RingSummaryDialog)]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hold", choices=("owner", "caller", "summary"))
    parser.add_argument("--summary-case", choices=("review", "matched", "unknown"), default="review")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="yanxi_verify_ui_") as folder, \
            patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden")), \
            patch("tkinter.messagebox.showerror", side_effect=AssertionError("Unexpected GUI error")):
        root = tk.Tk()
        store = SettingsStore(folder)
        store.load = lambda: DesktopSettings()
        fake = SimpleNamespace(close=lambda: None)
        app = DesktopApp(root, store=store, service=fake, prompt_setup=False)
        root.title("言犀核验 · 界面验收（虚构资料，无网络）")
        root.update()
        owner = VerificationOwnerDialog(app)
        owner.count.set(5)
        owner.category.set("工作业务")
        demo = ("小言 | 言老师", "示例大学", "计算机学院", "校园讲座预约", "示例活动中心", "周三下午", "青竹计划")
        for (key, *_), answer in zip(QUESTION_SPECS, demo):
            owner.enabled[key].set(True)
            owner.answers[key].set(answer)
        root.update()
        for size in ("1000x760", "820x620"):
            owner.geometry(size)
            root.update()
            assert owner.save_button.winfo_ismapped()
            assert owner.save_button.winfo_rooty() + owner.save_button.winfo_height() <= owner.winfo_rooty() + owner.winfo_height(), "Save clipped"
        owner.save_profile()
        assert not app.utility_busy
        encrypted = VerificationVault(folder).path.read_text(encoding="utf-8")
        assert "青竹计划" not in encrypted
        assert app.jobs == {}

        # A caller cannot see reference values or receive per-question feedback.
        caller = PreRingDialog(app)
        caller.category.set("工作业务")
        with patch.object(RingSummaryDialog, "bell") as bell:
            caller.begin()
            root.update()
            assert not bell.called and not rings(root)
            answers = {spec[2]: answer.split("|")[0].strip() for spec, answer in zip(QUESTION_SPECS, demo)}
            for index in range(5):
                assert not rings(root)
                prompt = caller.session.current_prompt
                assert "青竹计划" not in prompt
                caller.answer.set("不匹配" if index == 0 else "" if index == 1 else answers[prompt])
                caller.submit()
                root.update()
                if index < 4:
                    assert not bell.called
            assert bell.call_count == 1
        ring = rings(root)[0]
        for size in ("850x650", "740x550"):
            ring.geometry(size)
            root.update()
            for button in ring.decision_buttons:
                assert button.winfo_ismapped(), "Decision button hidden"
                assert button.winfo_rooty() + button.winfo_height() <= ring.winfo_rooty() + ring.winfo_height(), "Decision button clipped"
        text = "\n".join(label.cget("text") for label in ring.text_labels)
        assert "75%" in text and "80%" in text
        assert not any(value in text for value in demo)
        assert ring.summary.matched == 3 and ring.summary.mismatched == 1
        ring.choose("接听")
        assert ring.decision == "接听" and app.jobs == {}
        ring.destroy()
        assert not app.utility_busy
        assert not (Path(folder) / "data/call_logs").exists()

        # An incomplete session still yields a neutral owner card.
        caller = PreRingDialog(app)
        caller.category.set("工作业务")
        caller.begin()
        caller.cancel_call()
        ring = rings(root)[0]
        assert ring.summary.status == "cancelled" and ring.summary.answered == 0
        ring.destroy()
        caller = PreRingDialog(app)
        caller.category.set("工作业务")
        caller.begin()
        caller.session._deadline = -1
        caller.tick()
        assert rings(root)[0].summary.status == "timeout"
        rings(root)[0].destroy()

        # Caller-selected categories cannot expose out-of-scope reference facts.
        caller = PreRingDialog(app)
        caller.category.set("配送联系")
        caller.begin()
        assert rings(root)[0].summary.status == "unavailable"
        rings(root)[0].destroy()
        contacts = CallerStore(Path(folder) / "data/profiles/caller_profiles.json")
        contacts.set_contact("13800001111", display_name="虚构联系人")
        caller = PreRingDialog(app)
        caller.number.set("13800001111")
        caller.begin()
        assert rings(root)[0].summary.status == "known"
        rings(root)[0].destroy()
        root.update()
        assert not app.utility_busy and app.jobs == {}
        print("VERIFICATION_GUI_OK: encrypted setup, 5-question flow, no early ring, aggregate-only card, cancel/timeout, category and contact bypass", flush=True)
        if args.hold == "owner":
            held = VerificationOwnerDialog(app)
        elif args.hold == "caller":
            held = PreRingDialog(app)
            held.category.set("工作业务")
            held.begin()
        elif args.hold == "summary":
            from src.verification.core import VerificationSummary
            samples = {"review": VerificationSummary("complete", 5, 3, 1, 5),
                       "matched": VerificationSummary("complete", 5, 5, 0, 5),
                       "unknown": VerificationSummary("timeout", 5, 0, 0, 1)}
            held = RingSummaryDialog(app, samples[args.summary_case])
        if args.hold:
            held.title("言犀核验 · " + args.hold + " · 界面验收（虚构资料）")
            root.after(180_000, root.destroy)
            root.mainloop()
        else:
            app.close()


if __name__ == "__main__":
    main()
