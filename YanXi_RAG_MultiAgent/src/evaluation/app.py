"""Native Tk evaluation workbench; all network/ASR work stays off the UI thread."""
import os
import queue
import threading
import time
import tkinter as tk
from dataclasses import replace
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from src.desktop.audio import LocalTranscriber
from src.desktop.settings import ROOT, STT_MODELS, VOICES, SettingsStore
from .models import MODES, VERDICTS, discover_cases, metrics, write_json
from .reports import DISCLAIMER, load_report, save_report
from .presentation import RESULT_TABS, result_views
from .service import EvaluationService, new_run_directory, run_evaluation, test_connections
from .settings import DEFAULT_DATASET, EvaluationSettings, EvaluationSettingsStore

FILTERS = ("全部", "待评测", "通过", "不通过", "待复核", "运行异常", "仅数据集", "仅生成")
BG, INK, BLUE = "#f2f5fa", "#172b46", "#2563eb"


class SettingsDialog(tk.Toplevel):
    def __init__(self, parent, settings, store, on_save):
        super().__init__(parent)
        self.title("评测台 · API 与语音设置")
        self.geometry("760x780")
        self.minsize(700, 740)
        self.transient(parent)
        self.store, self.on_save = store, on_save
        self.testing, self.events, self.controls = False, queue.Queue(), []
        self.vars = {key: tk.StringVar(value=getattr(settings, key)) for key in (
            "api_key", "judge_api_key", "sut_model", "judge_model", "region", "stt_engine", "stt_model", "voice")}
        self.remember = tk.BooleanVar(value=settings.remember_key)
        self.columnconfigure(0, weight=1)
        body = ttk.Frame(self, padding=22)
        body.grid(sticky="nsew")
        body.columnconfigure(1, weight=1)
        ttk.Label(body, text="独立配置，不改动日常管家", style="Eval.Subtitle.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 14))
        rows = [("管家 API Key", "api_key", None), ("评审专用 Key（可留空）", "judge_api_key", None),
            ("管家模型", "sut_model", None), ("评审 / 生成模型", "judge_model", None),
            ("Qwen 服务区域", "region", ("中国内地", "国际 / 新加坡")),
            ("本地识别引擎", "stt_engine", ("whisper", "faster-whisper")),
            ("识别模型", "stt_model", STT_MODELS), ("合成音色", "voice", tuple(VOICES.values()))]
        for row, (label, key, choices) in enumerate(rows, 1):
            ttk.Label(body, text=label).grid(row=row, column=0, sticky="w", padx=(0, 14), pady=7)
            if choices:
                widget = ttk.Combobox(body, textvariable=self.vars[key], values=choices, state="readonly")
            else:
                widget = ttk.Entry(body, textvariable=self.vars[key], show="●" if "key" in key else "")
            widget.grid(row=row, column=1, sticky="ew", pady=7)
            self.controls.append(widget)
        reuse = ttk.Button(body, text="读取本机管家已保存的 Qwen Key", command=self.reuse_key)
        reuse.grid(row=9, column=0, columnspan=2, sticky="w", pady=(10, 4))
        self.controls.append(reuse)
        remember = ttk.Checkbutton(body, text="保存密钥到本机（默认；取消将移除评测台已保存的密钥）", variable=self.remember)
        remember.grid(row=10, column=0, columnspan=2, sticky="w", pady=6)
        self.controls.append(remember)
        ttk.Label(body, wraplength=660, text=(
            "评审 Key 留空时与管家共用 Key；仍是独立请求、独立提示词。"
            "可以使用不同评审模型减少同模型偏差。\n"
            "密钥保存在 data/evaluation/.env（本地明文、Git忽略）；分享工程时不要包含 data 或 .env。\n"
            "录音本地识别；转写/配套文本/管家输出会发送给Qwen。生成时会发送数据集名称与配套文本；"
            "选择合成语音时，生成的台词会发送给Edge TTS。"), style="Eval.Muted.TLabel").grid(
                row=11, column=0, columnspan=2, sticky="ew", pady=12)
        self.status = tk.StringVar(value="首次填写 Key 后保存即可，无需先测试连接。")
        ttk.Label(body, textvariable=self.status, wraplength=660, foreground=BLUE).grid(
            row=12, column=0, columnspan=2, sticky="ew", pady=8)
        actions = ttk.Frame(body)
        actions.grid(row=13, column=0, columnspan=2, sticky="ew", pady=10)
        test = ttk.Button(actions, text="测试两路 API（2次短请求）", command=self.test)
        test.pack(side="left")
        save = ttk.Button(actions, text="保存设置", command=self.save, style="Eval.Primary.TButton")
        save.pack(side="right")
        self.controls.extend((test, save))
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.after_id = self.after(100, self.poll)
        self.update_idletasks()
        self.geometry(f"+{max(0, parent.winfo_rootx() + (parent.winfo_width() - self.winfo_width()) // 2)}"
            f"+{max(0, parent.winfo_rooty() + 20)}")
        self.grab_set()

    def snapshot(self):
        settings = EvaluationSettings(**{key: value.get().strip() for key, value in self.vars.items()},
            remember_key=self.remember.get())
        settings.validate()
        return settings

    def reuse_key(self):
        self.vars["api_key"].set(SettingsStore().saved_key("qwen"))
        self.status.set("已读取（仍需点击保存）。" if self.vars["api_key"].get() else "未找到已保存的Qwen密钥，请手动填写。")

    def save(self):
        try:
            settings = self.snapshot()
            self.store.save(settings)
            self.on_save(settings)
            self.close()
        except Exception as exc:
            self.status.set(EvaluationSettings().safe_error(exc))

    def test(self):
        try:
            settings = self.snapshot()
        except ValueError as exc:
            self.status.set(str(exc))
            return
        self.testing = True
        for widget in self.controls:
            widget.state(["disabled"])
        self.status.set("正在依次测试管家与评审接口…")

        def worker():
            try:
                test_connections(settings)
                self.events.put("两路API均响应正常；请点击保存设置。")
            except Exception as exc:
                self.events.put(settings.safe_error(exc))
        threading.Thread(target=worker, daemon=True).start()

    def poll(self):
        try:
            result = self.events.get_nowait()
        except queue.Empty:
            pass
        else:
            self.testing = False
            for widget in self.controls:
                widget.state(["!disabled"])
            self.status.set(result)
        self.after_id = self.after(100, self.poll)

    def close(self):
        if self.testing:
            self.status.set("API测试尚未返回，请稍候；单路请求设置了超时。")
            return
        self.after_cancel(self.after_id)
        self.destroy()


class EvaluationApp:
    def __init__(self, root, store=None, auto_load=True, prompt_settings=True):
        self.root, self.store = root, store or EvaluationSettingsStore()
        self.settings = self.store.load()
        self.cases, self.controls, self.run_cases = [], [], []
        self.events, self.stop = queue.Queue(), threading.Event()
        self.busy, self.closing, self.last_directory = False, False, None
        self.generated_count = 0
        self.dialog = None
        root.title("言犀 AI 管家 · 独立评测台")
        root.geometry(f"{min(1450, root.winfo_screenwidth() - 80)}x{min(960, root.winfo_screenheight() - 110)}")
        root.minsize(1160, 760)
        root.configure(bg=BG)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._style()
        self._build()
        self.after_id = root.after(100, self.poll)
        if auto_load and DEFAULT_DATASET.is_dir():
            self.load_paths([DEFAULT_DATASET])
        if prompt_settings and not self.settings.api_key:
            root.after(250, self.open_settings)

    def _style(self):
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            tkfont.nametofont(name).configure(family="Microsoft YaHei UI", size=10)
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=INK)
        style.configure("TButton", padding=(10, 6))
        style.configure("TCheckbutton", background=BG)
        style.configure("Eval.Title.TLabel", font=("Microsoft YaHei UI", 20, "bold"))
        style.configure("Eval.Subtitle.TLabel", font=("Microsoft YaHei UI", 12, "bold"))
        style.configure("Eval.Muted.TLabel", foreground="#52677f")
        style.configure("Eval.Primary.TButton", background=BLUE, foreground="white")
        style.map("Eval.Primary.TButton", background=[("active", "#1d4ed8"), ("disabled", "#b8c5d8")])
        style.configure("Treeview", rowheight=32, background="white", fieldbackground="white")
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 10, "bold"), padding=6)
        style.configure("Eval.Result.TNotebook.Tab", padding=(6, 6), font=("Microsoft YaHei UI", 9))

    def button(self, parent, text, command, **kwargs):
        widget = ttk.Button(parent, text=text, command=command, **kwargs)
        self.controls.append(widget)
        return widget

    def _build(self):
        shell = ttk.Frame(self.root, padding=18)
        shell.pack(fill="both", expand=True)
        shell.columnconfigure(0, weight=1)
        shell.rowconfigure(4, weight=1)
        header = ttk.Frame(shell)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(header, text="言犀 · AI 管家评测台", style="Eval.Title.TLabel").pack(side="left")
        self.button(header, "API 与语音设置", self.open_settings).pack(side="right")
        self.button(header, "载入历史 JSON", self.load_history).pack(side="right", padx=8)
        ttk.Label(shell, text="单轮来电 · 独立大模型评审 · 四维评分与证据 · 不修改原录音和日常管家数据",
            style="Eval.Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(0, 10))

        notebook = ttk.Notebook(shell)
        notebook.grid(row=2, column=0, sticky="ew")
        dataset = ttk.Frame(notebook, padding=12)
        generated = ttk.Frame(notebook, padding=12)
        notebook.add(dataset, text="  录音数据集  ")
        notebook.add(generated, text="  生成新来电  ")
        dataset.columnconfigure(0, weight=1)
        self.dataset_path = tk.StringVar(value=str(DEFAULT_DATASET))
        path_entry = ttk.Entry(dataset, textvariable=self.dataset_path)
        path_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.controls.append(path_entry)
        self.button(dataset, "选择文件夹", self.choose_folder).grid(row=0, column=1, padx=4)
        self.button(dataset, "加载此目录", lambda: self.load_paths([self.dataset_path.get()])).grid(row=0, column=2, padx=4)
        self.button(dataset, "添加音频", self.choose_files).grid(row=0, column=3, padx=4)
        options = ttk.Frame(dataset)
        options.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        ttk.Label(options, text="导入模式").pack(side="left")
        self.dataset_mode = tk.StringVar(value=MODES["audio"])
        mode = ttk.Combobox(options, state="readonly", width=24, textvariable=self.dataset_mode,
            values=(MODES["audio"], MODES["reference"]))
        mode.pack(side="left", padx=8)
        self.controls.append(mode)
        ttk.Label(options, text="递归读取音频；自动匹配同名.txt或“文本”子目录。模式仅影响新导入条目。",
            style="Eval.Muted.TLabel").pack(side="left")
        self.dataset_info = tk.StringVar(value="尚未加载数据集")
        ttk.Label(dataset, textvariable=self.dataset_info, style="Eval.Muted.TLabel").grid(
            row=2, column=0, columnspan=4, sticky="w", pady=(8, 0))

        generated.columnconfigure(1, weight=1)
        ttk.Label(generated, text="场景方向").grid(row=0, column=0, sticky="w")
        self.focus = tk.StringVar(value="现有数据集之外的真实生活场景，兼顾正常、模糊、紧急与安全边界")
        focus = ttk.Entry(generated, textvariable=self.focus)
        focus.grid(row=0, column=1, columnspan=3, sticky="ew", padx=(8, 0))
        self.controls.append(focus)
        opts = ttk.Frame(generated)
        opts.grid(row=1, column=0, columnspan=4, sticky="ew", pady=8)
        ttk.Label(opts, text="数量").pack(side="left")
        self.generate_count = tk.StringVar(value="10")
        count = ttk.Spinbox(opts, from_=1, to=200, width=5, textvariable=self.generate_count)
        count.pack(side="left", padx=8)
        self.controls.append(count)
        self.generate_mode = tk.StringVar(value=MODES["text"])
        mode = ttk.Combobox(opts, state="readonly", width=23, textvariable=self.generate_mode,
            values=(MODES["text"], MODES["tts"]))
        mode.pack(side="left", padx=8)
        self.controls.append(mode)
        self.button(opts, "生成并去重 → 加入队列", self.generate, style="Eval.Primary.TButton").pack(side="left", padx=8)
        ttk.Label(generated, text="先加载原数据集，再生成；生成后切到“仅生成”筛选，点击“评测待处理”。语音模式会额外调用Edge TTS。",
            style="Eval.Muted.TLabel").grid(row=2, column=0, columnspan=4, sticky="w")

        overview = ttk.Frame(shell)
        overview.grid(row=3, column=0, sticky="ew", pady=12)
        self.summary = tk.StringVar(value="请先加载来电案例")
        ttk.Label(overview, textvariable=self.summary, style="Eval.Subtitle.TLabel").pack(side="left")
        self.filter = tk.StringVar(value="全部")
        combo = ttk.Combobox(overview, state="readonly", textvariable=self.filter, values=FILTERS, width=11)
        combo.pack(side="right")
        combo.bind("<<ComboboxSelected>>", lambda event: self.refresh())
        ttk.Label(overview, text="筛选  ").pack(side="right")

        self.panes = ttk.Panedwindow(shell, orient="horizontal")
        self.panes.grid(row=4, column=0, sticky="nsew")
        left, right = ttk.Frame(self.panes), ttk.Frame(self.panes)
        self.panes.add(left, weight=1)
        self.panes.add(right, weight=1)
        left.columnconfigure(0, weight=1)
        left.rowconfigure(0, weight=1)
        columns = ("name", "mode", "status", "score")
        self.tree = ttk.Treeview(left, columns=columns, show="headings", selectmode="extended", height=9)
        for key, label, width in (("name", "来电场景", 260), ("mode", "模式", 135),
                                 ("status", "结果 / 状态", 105), ("score", "分数", 55)):
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, minwidth=50, stretch=key == "name")
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(left, orient="horizontal", command=self.tree.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=scroll.set, xscrollcommand=horizontal.set)
        for tag, color in (("pass", "#e8f6ef"), ("fail", "#fcebec"), ("review", "#fff5d9"), ("error", "#ffeddc")):
            self.tree.tag_configure(tag, background=color)
        self.tree.bind("<<TreeviewSelect>>", lambda event: self.show_selected())
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)
        detail_header = ttk.Frame(right)
        detail_header.grid(row=0, column=0, sticky="ew", padx=(10, 0), pady=4)
        detail_header.columnconfigure(0, weight=1)
        self.detail_title = tk.StringVar(value="选择案例查看结果")
        ttk.Label(detail_header, textvariable=self.detail_title, wraplength=365,
            style="Eval.Subtitle.TLabel").grid(row=0, column=0, sticky="w")
        # Reading/copying results stays available while a batch runs.
        self.copy_result_button = ttk.Button(detail_header, text="复制当前页", command=self.copy_result, state="disabled")
        self.copy_result_button.grid(row=0, column=1, sticky="e", padx=(8, 0))
        self.result_notebook = ttk.Notebook(right, style="Eval.Result.TNotebook")
        self.result_notebook.grid(row=1, column=0, sticky="nsew", padx=(10, 0))
        self.result_texts, self.result_pages = {}, {}
        self._detail_case_id = None
        for key, label in RESULT_TABS.items():
            page = ttk.Frame(self.result_notebook)
            page.columnconfigure(0, weight=1)
            page.rowconfigure(0, weight=1)
            self.result_notebook.add(page, text=label)
            view = ScrolledText(page, wrap="word", width=34, height=10, font=("Microsoft YaHei UI", 10),
                bg="white", fg=INK, relief="flat", padx=14, pady=10, state="disabled")
            view.grid(row=0, column=0, sticky="nsew")
            self.result_texts[key], self.result_pages[key] = view, str(page)
        self._set_details("选择左侧案例，再按页签查看管家回复、评测评价、评分依据与判断过程。")
        bottom = ttk.Frame(shell)
        bottom.grid(row=5, column=0, sticky="ew", pady=(12, 0))
        self.button(bottom, "评测待处理（当前筛选）", lambda: self.start("pending"), style="Eval.Primary.TButton").pack(side="left")
        self.button(bottom, "评测选中", lambda: self.start("selected")).pack(side="left", padx=6)
        self.button(bottom, "重试异常", lambda: self.start("errors")).pack(side="left")
        self.stop_button = ttk.Button(bottom, text="完成当前后停止", command=self.request_stop, state="disabled")
        self.stop_button.pack(side="left", padx=6)
        self.button(bottom, "导出当前队列", self.export).pack(side="right")
        self.button(bottom, "打开最近报告", self.open_report).pack(side="right", padx=6)
        self.button(bottom, "移除选中", self.remove_selected).pack(side="right")
        self.progress = ttk.Progressbar(shell, mode="determinate", maximum=1)
        self.progress.grid(row=6, column=0, sticky="ew", pady=(12, 5))
        self.status = tk.StringVar(value="就绪 · API密钥仅保存在本机；报告不含密钥。")
        ttk.Label(shell, textvariable=self.status, wraplength=1320, style="Eval.Muted.TLabel").grid(row=7, column=0, sticky="w")
        ttk.Label(shell, text="通过率仅统计可判定结果；复核/异常/降级另列。模型评审有误差，重要结论请人工核查。",
            style="Eval.Muted.TLabel").grid(row=8, column=0, sticky="w", pady=(4, 0))

    @staticmethod
    def _set_result_text(widget, text, preserve_scroll=False):
        if widget.get("1.0", "end-1c") == text:
            return
        position = widget.yview()[0] if preserve_scroll else 0
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")
        widget.yview_moveto(position)

    def _set_details(self, text):
        """Clear every page, including raw data, when no case is selected."""
        self._detail_case_id = None
        self.detail_title.set("选择案例查看结果")
        self.copy_result_button.state(["disabled"])
        for key, widget in self.result_texts.items():
            self._set_result_text(widget, text + ("\n\n" + DISCLAIMER if key == "runtime" else ""))

    def copy_result(self):
        if self._detail_case_id is None:
            return
        selected = self.result_notebook.select()
        key = next((key for key, page in self.result_pages.items() if page == selected), None)
        if key is not None:
            try:
                self.root.clipboard_clear()
                self.root.clipboard_append(self.result_texts[key].get("1.0", "end-1c"))
                self.status.set("已复制：" + RESULT_TABS[key])
            except tk.TclError:
                self.status.set("暂时无法访问剪贴板，请在当前页选中文字后复制。")

    def visible_cases(self):
        kind = self.filter.get()
        return [c for c in self.cases if kind == "全部"
            or kind == "待评测" and c.status == "待评测"
            or kind == "运行异常" and c.status in ("错误", "管家降级")
            or kind == "仅数据集" and c.source == "dataset"
            or kind == "仅生成" and c.source == "generated"
            or c.status == "已评审" and VERDICTS.get(c.judgment.get("verdict")) == kind]

    def refresh(self):
        selected = self.tree.selection()
        view = self.tree.yview()
        self.tree.delete(*self.tree.get_children())
        for case in self.visible_cases():
            label = VERDICTS.get(case.judgment.get("verdict"), "") if case.status == "已评审" else case.status
            tag = "error" if case.status in ("错误", "管家降级") else case.judgment.get("verdict", "")
            self.tree.insert("", "end", iid=case.id, values=(case.name, MODES[case.mode], label,
                case.judgment.get("score", "—")), tags=(tag,))
        retained = [item for item in selected if self.tree.exists(item)]
        if retained:
            self.tree.selection_set(retained)
        if view:
            self.tree.yview_moveto(view[0])
        m = metrics(self.cases)
        rate = f"{m['pass_rate']}%" if m["pass_rate"] is not None else "—"
        self.summary.set(f"共{m['total']} · 通过{m['pass']} · 不通过{m['fail']} · 复核{m['review']} · 异常{m['error'] + m['degraded']} · 通过率{rate}")
        if self.run_cases:
            self.progress.configure(maximum=len(self.run_cases), value=sum(c.status not in ("待评测", "处理中") for c in self.run_cases))
        self.show_selected()

    def show_selected(self):
        selected = self.tree.selection()
        if selected:
            case = next((c for c in self.cases if c.id == selected[0]), None)
            if case:
                same_case = self._detail_case_id == case.id
                self._detail_case_id = case.id
                self.detail_title.set(f"{case.name[:45]}{'…' if len(case.name) > 45 else ''} · {case.status}")
                self.copy_result_button.state(["!disabled"])
                for key, content in result_views(case).items():
                    self._set_result_text(self.result_texts[key], content, preserve_scroll=same_case)
                return
        self._set_details("未选中案例。请选择左侧条目；被筛选隐藏或移除的案例不会继续显示在这里。")

    def load_paths(self, paths):
        if self.busy:
            return
        try:
            mode = "audio" if self.dataset_mode.get() == MODES["audio"] else "reference"
            added = discover_cases(paths, mode)
            identities = {(c.audio_path.casefold(), c.mode) for c in self.cases if c.source == "dataset"}
            fresh = [c for c in added if (c.audio_path.casefold(), c.mode) not in identities]
            self.cases.extend(fresh)
            dataset = [c for c in self.cases if c.source == "dataset"]
            self.dataset_info.set(f"本次新增 {len(fresh)} 条；队列数据集 {len(dataset)} 条，其中有配套文本 {sum(bool(c.reference) for c in dataset)} 条。")
            self.filter.set("全部")
            self.refresh()
            self.status.set("加载完成。建议先选2~3条小样本评测，再运行整批。" if added else "未找到音频，请检查目录。")
        except Exception as exc:
            messagebox.showerror("加载失败", self.settings.safe_error(exc), parent=self.root)

    def choose_folder(self):
        selected = filedialog.askdirectory(parent=self.root, initialdir=self.dataset_path.get())
        if selected:
            self.dataset_path.set(selected)
            self.load_paths([selected])

    def choose_files(self):
        files = filedialog.askopenfilenames(parent=self.root, title="选择来电录音", filetypes=[
            ("音频", "*.wav *.mp3 *.m4a *.flac *.ogg *.aac *.wma *.mp4 *.webm *.aiff"), ("全部文件", "*.*")])
        if files:
            self.load_paths(files)

    def open_settings(self):
        if self.busy:
            return
        if self.dialog and self.dialog.winfo_exists():
            self.dialog.lift()
            return
        self.dialog = SettingsDialog(self.root, self.settings, self.store, self.settings_saved)

    def settings_saved(self, settings):
        self.settings = settings
        self.status.set(f"设置已保存 · 管家 {settings.sut_model} · 评审 {settings.judge_model} · {settings.region}")

    def require_settings(self):
        try:
            self.settings.validate()
            return True
        except ValueError:
            self.open_settings()
            return False

    def _worker(self, operation, function):
        self.busy = True
        self.stop.clear()
        for widget in self.controls:
            widget.state(["disabled"])
        self.stop_button.state(["!disabled"])
        self.status.set(operation)

        def worker():
            try:
                function()
            except Exception as exc:
                self.events.put(("error", self.settings.safe_error(exc)))
            finally:
                self.events.put(("done", operation))
        threading.Thread(target=worker, name="yanxi-evaluator", daemon=True).start()

    def start(self, which):
        if self.busy or not self.require_settings():
            return
        candidates = self.visible_cases()
        if which == "selected":
            ids = set(self.tree.selection())
            selected = [c for c in candidates if c.id in ids]
        elif which == "errors":
            selected = [c for c in candidates if c.status in ("错误", "管家降级")]
        else:
            selected = [c for c in candidates if c.status == "待评测"]
        if not selected:
            messagebox.showinfo("没有待执行案例", "请检查当前筛选条件或选择左侧案例。", parent=self.root)
            return
        counts = {label: sum(c.mode == key for c in selected) for key, label in MODES.items()}
        modes = "\n".join(f"{label}：{count}条" for label, count in counts.items() if count)
        if not messagebox.askyesno("确认评测与API调用", f"将执行 {len(selected)} 条：\n{modes}\n\n"
            "转写/配套文本及管家输出会发送给Qwen。每条另需1次独立评审；管家按自身规则决定API调用。"
            "合成语音额外使用Edge TTS。会产生API费用。\n"
            "重测会更新界面结果，旧运行报告仍保留。现在开始？", parent=self.root):
            return
        allow_download = False
        if any(c.mode in ("audio", "tts") for c in selected):
            transcriber = LocalTranscriber(self.settings.stt_model, self.settings.stt_engine)
            if not transcriber.is_cached():
                allow_download = messagebox.askyesno("识别模型未缓存", "是否允许下载选定识别模型？可能需要较多时间和磁盘空间。", parent=self.root)
                if not allow_download:
                    return
        try:
            directory = new_run_directory()
        except OSError as exc:
            messagebox.showerror("无法保存报告", self.settings.safe_error(exc), parent=self.root)
            return
        self.last_directory, self.run_cases = directory, selected
        for case in selected:
            case.status = "待评测"
        settings = replace(self.settings)
        self.refresh()

        def work():
            service = EvaluationService(settings, directory)
            metadata = run_evaluation(selected, service, self.stop, lambda k, v: self.events.put((k, v)), allow_download)
            self.events.put(("progress", (metadata["stop_reason"] or "本批已结束") + f"。报告：{directory}"))
        self._worker("评测运行中", work)

    def generate(self):
        if self.busy or not self.require_settings():
            return
        if not any(c.source == "dataset" for c in self.cases):
            messagebox.showinfo("先加载数据集", "生成前需要原数据集作为去重基准。", parent=self.root)
            return
        try:
            count = int(self.generate_count.get())
            if not 1 <= count <= 200:
                raise ValueError()
        except ValueError:
            messagebox.showerror("数量无效", "请输入1~200之间的整数。", parent=self.root)
            return
        if not messagebox.askyesno("确认生成与语义去重", f"生成目标：{count} 条新来电。\n\n"
            "将把已加载的数据集名称与配套文本发送给评审模型，用于生成和语义去重，会产生API费用。"
            "去重失败会有限重试，可能生成不足；模型无法保证绝对不重复。\n"
            "生成后仅加入队列，不自动测试、不真实拨打电话。继续？", parent=self.root):
            return
        existing, settings, focus = list(self.cases), replace(self.settings), self.focus.get()
        mode = "tts" if self.generate_mode.get() == MODES["tts"] else "text"
        try:
            directory = new_run_directory()
        except OSError as exc:
            messagebox.showerror("无法保存来电", self.settings.safe_error(exc), parent=self.root)
            return
        self.last_directory = directory
        self.filter.set("仅生成")
        self.run_cases = []
        self.generated_count = 0
        self.progress.configure(value=0, maximum=count)
        self.refresh()

        def work():
            service = EvaluationService(settings, directory)
            accepted = []
            metadata = {"run_id": directory.name, "settings": settings.public_dict(), "operation": "generate",
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S%z"), "focus": focus, "requested": count}

            def accept(case):
                case.mode = mode
                case.run_id = directory.name
                accepted.append(case)
                save_report(directory, accepted, metadata, full=False)
                self.events.put(("generated", case))
            try:
                metadata["generation"] = service.reviewer.generate(existing, count, focus, self.stop,
                    lambda message: self.events.put(("progress", message)), accept)
                write_json(directory / "generation.json", metadata["generation"])
                result = metadata["generation"]
                self.events.put(("notice", f"生成结束：接纳 {result['accepted']}/{count} 条，拒绝重复/无效 {result['rejected']} 条。"
                    "请检查新来电后点击“评测待处理（当前筛选）”。"))
            except Exception as exc:
                metadata["error"] = settings.safe_error(exc)
                raise
            finally:
                try:
                    save_report(directory, accepted, metadata, full=True)
                finally:
                    service.close()
        self._worker("正在生成与去重", work)

    def poll(self):
        changed = False
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "case":
                    changed = True
                elif kind == "generated":
                    self.cases.append(value)
                    self.generated_count += 1
                    self.progress.configure(value=self.generated_count)
                    changed = True
                elif kind in ("progress", "notice"):
                    self.status.set(value)
                elif kind == "error":
                    self.status.set("任务异常：" + value)
                    messagebox.showerror("任务未全部完成", value + "\n已保存的结果仍在本次报告目录。", parent=self.root)
                elif kind == "done":
                    self.busy = False
                    for widget in self.controls:
                        widget.state(["!disabled"])
                    self.stop_button.state(["disabled"])
                    changed = True
                    if self.closing:
                        self.root.destroy()
                        return
        except queue.Empty:
            pass
        if changed:
            self.refresh()
        self.after_id = self.root.after(100, self.poll)

    def request_stop(self):
        self.stop.set()
        self.stop_button.state(["disabled"])
        self.status.set("已请求停止：等待当前识别/请求/案例完成，不启动下一条。结果会继续保存。")

    def remove_selected(self):
        ids = set(self.tree.selection())
        self.cases = [c for c in self.cases if c.id not in ids]
        self.refresh()
        self._set_details("已从当前队列移除选中项；磁盘上的原音频和报告未删除。")

    def export(self):
        if not self.cases:
            return
        selected = filedialog.askdirectory(parent=self.root, title="选择导出位置（自动创建独立子目录）")
        if not selected:
            return
        try:
            import uuid
            directory = Path(selected) / ("言犀评测报告_" + time.strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6])
            directory.mkdir(parents=True, exist_ok=False)
            save_report(directory, self.cases, {"run_id": directory.name, "operation": "queue_export",
                "note": "混合队列汇总；各案例run_id指向原始运行配置，不使用当前设置冒充历史配置。"})
            self.last_directory = directory
            self.status.set(f"已导出 JSON / CSV / HTML：{directory}")
        except Exception as exc:
            messagebox.showerror("导出失败", self.settings.safe_error(exc), parent=self.root)

    def load_history(self):
        path = filedialog.askopenfilename(parent=self.root, title="载入历史 report.json", filetypes=[("评测报告", "*.json")])
        if not path:
            return
        try:
            cases, _ = load_report(path)
            if self.cases and not messagebox.askyesno("替换当前队列", "载入报告将替换当前界面队列，不删除磁盘文件。继续？", parent=self.root):
                return
            self.cases, self.run_cases = cases, []
            self.last_directory = Path(path).parent
            self.filter.set("全部")
            self.refresh()
            self.status.set("已载入历史。可继续待评测案例或选中重测；音频移动后请重新导入。")
        except Exception as exc:
            messagebox.showerror("载入失败", self.settings.safe_error(exc), parent=self.root)

    def open_report(self):
        if not self.last_directory:
            messagebox.showinfo("暂无报告", "先完成一批评测、生成或导出。", parent=self.root)
            return
        path = self.last_directory / "report.html"
        if not path.is_file():
            messagebox.showinfo("报告尚未生成", "本批结束后会生成HTML报告。", parent=self.root)
            return
        try:
            os.startfile(str(path))
        except OSError as exc:
            messagebox.showerror("打开失败", self.settings.safe_error(exc), parent=self.root)

    def close(self):
        if self.busy:
            if messagebox.askyesno("任务仍在运行", "是否完成当前任务、保存结果后关闭窗口？", parent=self.root):
                self.closing = True
                self.request_stop()
            return
        if self.dialog and self.dialog.winfo_exists() and self.dialog.testing:
            self.dialog.lift()
            return
        self.root.after_cancel(self.after_id)
        self.root.destroy()


def main():
    root = tk.Tk()
    EvaluationApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
