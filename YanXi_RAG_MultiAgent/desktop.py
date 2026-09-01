"""Run with .venv/Scripts/python.exe desktop.py or py -3.12 desktop.py."""
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
        from src.desktop.app import main as desktop_main
        desktop_main()
    except ImportError as error:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("缺少桌面依赖", f"缺少模块：{error.name}\n\n请在工程目录运行：\n{sys.executable} -m pip install -r requirements-desktop.txt")
        root.destroy()
        raise SystemExit(1)


if __name__ == "__main__":
    main()
