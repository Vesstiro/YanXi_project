"""Portable entry point for the independent evaluation GUI."""
import os
import sys
from pathlib import Path


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    os.chdir(Path(__file__).resolve().parent)
    os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
    try:
        from src.evaluation.app import main as evaluation_main
        evaluation_main()
    except ImportError as exc:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("缺少评测依赖", f"缺少模块：{exc.name}\n\n请在工程目录执行：\n"
            f'"{sys.executable}" -m pip install -r requirements-desktop.txt')
        root.destroy()
        raise SystemExit(1)


if __name__ == "__main__":
    main()
