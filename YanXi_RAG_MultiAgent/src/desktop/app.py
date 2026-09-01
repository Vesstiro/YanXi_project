"""Tk desktop workspace. Workers communicate exclusively through queues."""
import json
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from tkinter import font as tkfont

from src.desktop.audio import AUDIO_EXTENSIONS, AudioSpeaker, MicrophoneRecorder, collect_audio
from src.desktop.service import DesktopService, Job, readable_result, reasoning_details, run_batch, speech_text
from src.desktop.settings import (DEFAULT_MODELS, PROVIDERS, ROOT, STT_MODELS, VOICES,
    DesktopSettings, SettingsStore, safe_error)

BG, PANEL, INK, MUTED, BLUE = "#F2F5FA", "#FFFFFF", "#15263F", "#65758B", "#2563EB"


class SettingsDialog(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app.root)
        self.app, self.events, self.testing = app, queue.Queue(), False
        self.title("API 与语音设置")
        self.geometry("660x780")
        self.resizable(False, False)
        self.transient(app.root)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        s = app.settings
        self.provider = tk.StringVar(value=s.provider)
        self.model = tk.StringVar(value=s.model)
        self.region = tk.StringVar(value=s.region)
        self.key = tk.StringVar(value=s.api_key)
        self.remember = tk.BooleanVar(value=s.remember_key)
        self.engine = tk.StringVar(value=s.stt_engine)
        self.stt = tk.StringVar(value=s.stt_model)
        self.voice = tk.StringVar(value=next((k for k, v in VOICES.items() if v == s.voice), next(iter(VOICES))))
        box = ttk.Frame(self, padding=24)
        box.pack(fill="both", expand=True)
        box.columnconfigure(1, weight=1)
        ttk.Label(box, text="连接你的模型服务", style="Title.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        ttk.Label(box, text="保存后立即应用；密钥不会出现在结果或导出文件里。", style="Muted.TLabel").grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 18))
        self.fields = []
        def field(row, label, variable, values=None):
            ttk.Label(box, text=label).grid(row=row, column=0, sticky="w", padx=(0, 20), pady=8)
            widget = ttk.Combobox(box, textvariable=variable, values=values, state="readonly") if values else ttk.Entry(box, textvariable=variable)
            widget.grid(row=row, column=1, sticky="ew", pady=8)
            self.fields.append((widget, "readonly" if values else "normal"))
            return widget
        # RAG embeddings use the selected Qwen key as well as its region.
        provider = field(2, "模型服务", self.provider, ["qwen"])
        provider.bind("<<ComboboxSelected>>", self.provider_changed)
        field(3, "模型名称", self.model)
        field(4, "Qwen 区域", self.region, ["中国内地", "国际 / 新加坡"])
        key_entry = field(5, "API Key", self.key)
        key_entry.configure(show="*")
        self.show_key = tk.BooleanVar(value=False)
        ttk.Checkbutton(box, text="显示密钥", variable=self.show_key,
            command=lambda: key_entry.configure(show="" if self.show_key.get() else "*")).grid(row=6, column=1, sticky="w")
        ttk.Checkbutton(box, text="保存到本机（默认）", variable=self.remember).grid(row=7, column=1, sticky="w", pady=(8, 0))
        ttk.Label(box, text="保存在项目 .env 明文文件中，已被 Git 忽略。\n取消勾选：仅本次使用，并移除该服务已保存的密钥。",
            style="Muted.TLabel", wraplength=420).grid(row=8, column=0, columnspan=2, sticky="w", pady=(8, 14))
        ttk.Separator(box).grid(row=9, column=0, columnspan=2, sticky="ew", pady=4)
        field(10, "本地识别引擎", self.engine, ["whisper", "faster-whisper"])
        field(11, "识别模型", self.stt, list(STT_MODELS))
        field(12, "播报音色", self.voice, list(VOICES))
        ttk.Label(box, text="whisper + turbo 参考 PythonProject2；small 更省资源。\n本版 RAG 使用同一 Qwen Key 调用嵌入接口，会消耗额度。\n首次下载识别模型需确认。Edge TTS 播报需要联网。",
            style="Muted.TLabel", wraplength=540).grid(row=13, column=0, columnspan=2, sticky="w", pady=10)
        self.hint = ttk.Label(box, text="", wraplength=550, style="Muted.TLabel")
        self.hint.grid(row=14, column=0, columnspan=2, sticky="w", pady=8)
        buttons = ttk.Frame(box)
        buttons.grid(row=15, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        self.test_button = ttk.Button(buttons, text="测试连接（少量额度）", command=self.test)
        self.test_button.pack(side="left")
        self.save_button = ttk.Button(buttons, text="保存并应用", style="Accent.TButton", command=self.save)
        self.save_button.pack(side="right")
        ttk.Button(buttons, text="取消", command=self.cancel).pack(side="right", padx=8)
        self._poll_id = self.after(100, self.poll)

    def provider_changed(self, _event=None):
        provider = self.provider.get()
        self.model.set(DEFAULT_MODELS[provider])
        self.key.set(self.app.store.saved_key(provider))

    def snapshot(self):
        result = DesktopSettings(provider=self.provider.get(), model=self.model.get().strip(),
            region=self.region.get(), api_key=self.key.get().strip(), remember_key=self.remember.get(),
            stt_engine=self.engine.get(), stt_model=self.stt.get(), voice=VOICES[self.voice.get()])
        result.validate()
        return result

    def test(self):
        try:
            settings = self.snapshot()
        except ValueError as error:
            messagebox.showwarning("配置未完成", str(error), parent=self)
            return
        self.testing = True
        self.test_button.configure(state="disabled")
        self.save_button.configure(state="disabled")
        for widget, _state in self.fields:
            widget.configure(state="disabled")
        self.hint.configure(text="正在验证连接，请稍候……")
        def worker():
            try:
                message = DesktopService(settings).test_connection()
            except Exception as error:
                message = "连接失败：" + safe_error(error, settings.api_key)
            self.events.put(message)
        threading.Thread(target=worker, daemon=True).start()

    def poll(self):
        try:
            self.hint.configure(text=self.events.get_nowait())
            self.testing = False
            self.test_button.configure(state="normal")
            self.save_button.configure(state="normal")
            for widget, state in self.fields:
                widget.configure(state=state)
        except queue.Empty:
            pass
        self._poll_id = self.after(100, self.poll)

    def destroy(self):
        if hasattr(self, "_poll_id"):
            self.after_cancel(self._poll_id)
        super().destroy()

    def save(self):
        if self.testing:
            return
        try:
            settings = self.snapshot()
            self.app.store.save(settings)
            self.app.apply_settings(settings)
        except Exception as error:
            messagebox.showerror("设置保存失败", safe_error(error, self.key.get()), parent=self)
            return
        self.destroy()

    def cancel(self):
        if self.testing:
            self.hint.configure(text="请等待本次连接测试结束后再关闭。")
        else:
            self.destroy()


class DesktopApp:
    def __init__(self, root, store=None, service=None, prompt_setup=True):
        self.root, self.store = root, store or SettingsStore()
        self.settings = self.store.load()
        self.service = service or DesktopService(self.settings, root=self.store.root)
        self.events, self.jobs = queue.Queue(), {}
        self.workers = []
        self.stop_batch, self.stop_record, self.stop_speech = (threading.Event() for _ in range(3))
        self.processing = self.recording = self.playing = self.utility_busy = self.closing = False
        self.download_allowed = False
        self.devices = {"系统默认麦克风": None}
        self.idle_controls, self.idle_combos = [], []
        self._owner_refresh_at = 0.0
        self.root.title("言犀 · RAG 多 Agent 通话工作台")
        width = min(1320, self.root.winfo_screenwidth() - 80)
        height = min(840, self.root.winfo_screenheight() - 100)
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(min(1120, width), min(700, height))
        self.root.configure(bg=BG)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._style()
        self._build()
        self.refresh_controls()
        self.root.after(80, self.poll)
        if prompt_setup and not self.settings.api_key:
            self.root.after(250, self.open_settings)

    def _style(self):
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont"):
            tkfont.nametofont(name).configure(family="Microsoft YaHei UI", size=10)
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", font=("Microsoft YaHei UI", 10), foreground=INK, background=BG)
        style.configure("TButton", padding=(12, 8), borderwidth=0)
        style.configure("Accent.TButton", background=BLUE, foreground="white", padding=(15, 9))
        style.map("Accent.TButton", background=[("disabled", "#A9BADD"), ("active", "#1D4ED8")])
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 19, "bold"))
        style.configure("Heading.TLabel", font=("Microsoft YaHei UI", 12, "bold"))
        style.configure("Muted.TLabel", foreground=MUTED)
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL, rowheight=36, borderwidth=0)
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 10, "bold"), padding=8)
        style.map("Treeview", background=[("selected", "#DBEAFE")], foreground=[("selected", INK)])
        style.configure("TNotebook.Tab", padding=(18, 9))
        style.configure("TEntry", padding=6)
        style.configure("TProgressbar", background=BLUE, troughcolor="#E2E8F0", borderwidth=0)

    def button(self, parent, text, command, accent=False, idle=True, **pack):
        button = ttk.Button(parent, text=text, command=command, style="Accent.TButton" if accent else "TButton")
        button.pack(**pack)
        if idle:
            self.idle_controls.append(button)
        return button

    def _build(self):
        sidebar = tk.Frame(self.root, bg="#142A46", width=195)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        tk.Label(sidebar, text="言 犀", bg="#142A46", fg="white", font=("Microsoft YaHei UI", 28, "bold")).pack(anchor="w", padx=25, pady=(30, 0))
        tk.Label(sidebar, text="AI 通话工作台", bg="#142A46", fg="#A6BCD7", font=("Microsoft YaHei UI", 11)).pack(anchor="w", padx=25, pady=(4, 32))
        self.provider_label = tk.Label(sidebar, text="", bg="#203D61", fg="#E2EDFF", justify="left", padx=12, pady=12,
            wraplength=150, font=("Microsoft YaHei UI", 9))
        self.provider_label.pack(fill="x", padx=15)
        self.button(sidebar, "API 与语音设置", self.open_settings, fill="x", padx=15, pady=(12, 8))
        self.context_button = self.button(sidebar, "机主与号码管理", self.open_context, fill="x", padx=15, pady=4)
        self.owner_label = tk.Label(sidebar, text="", bg="#142A46", fg="#D7E5F7", justify="left",
            wraplength=155, font=("Microsoft YaHei UI", 9))
        self.owner_label.pack(fill="x", padx=20, pady=(8, 8))
        self.button(sidebar, "使用说明", self.help, idle=False, fill="x", padx=15, pady=4)
        tk.Label(sidebar, text="知识 / 转写 → Qwen\n播报 → Edge TTS\n密钥与个人记录请勿分享",
            bg="#142A46", fg="#A6BCD7", justify="left", wraplength=156, font=("Microsoft YaHei UI", 9)).pack(side="bottom", padx=20, pady=18)
        main = ttk.Frame(self.root, padding=(22, 22, 22, 14))
        main.pack(side="left", fill="both", expand=True)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(5, weight=1)
        ttk.Label(main, text="把每一通来电，处理得明明白白", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(main, text="麦克风 / 本地音频 / 批量处理     ·     来电分析原型，不接入真实电话线路", style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(6, 18))
        common = ttk.Frame(main)
        common.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(common, text="1  选择输入方式", style="Heading.TLabel").pack(side="left")
        self.caller = tk.StringVar()
        caller = ttk.Entry(common, textvariable=self.caller, width=20)
        caller.pack(side="right")
        self.idle_controls.append(caller)
        ttk.Label(common, text="来电号码（选填）  ", style="Muted.TLabel").pack(side="right")
        input_tabs = ttk.Notebook(main, height=151)
        input_tabs.grid(row=3, column=0, sticky="ew")
        mic, files, text = (ttk.Frame(input_tabs, padding=14) for _ in range(3))
        input_tabs.add(mic, text="麦克风录音")
        input_tabs.add(files, text="本地音频 / 批量")
        input_tabs.add(text, text="文字输入")
        row = ttk.Frame(mic)
        row.pack(fill="x")
        self.device = tk.StringVar(value="系统默认麦克风")
        self.device_combo = ttk.Combobox(row, textvariable=self.device, values=list(self.devices), state="readonly", width=53)
        self.device_combo.pack(side="left", fill="x", expand=True)
        self.idle_combos.append(self.device_combo)
        self.button(row, "刷新设备", self.refresh_devices, side="left", padx=(8, 0))
        row = ttk.Frame(mic)
        row.pack(fill="x", pady=(12, 6))
        self.button(row, "开始录音", self.start_recording, accent=True, side="left")
        self.record_stop_button = self.button(row, "停止并加入队列", self.stop_record.set, idle=False, side="left", padx=8)
        self.mic_time = ttk.Label(row, text="00:00", style="Heading.TLabel")
        self.mic_time.pack(side="left", padx=8)
        self.mic_meter = ttk.Progressbar(row, maximum=100, length=160)
        self.mic_meter.pack(side="left", padx=10)
        self.auto_process_mic = tk.BooleanVar(value=True)
        ttk.Checkbutton(mic, text="停止录音后自动处理队列（最长录音 10 分钟）", variable=self.auto_process_mic).pack(anchor="w")
        ttk.Label(files, text="可选择一个或多个文件，也可导入文件夹第一层中的全部音频。", style="Muted.TLabel").pack(anchor="w", pady=(2, 12))
        row = ttk.Frame(files)
        row.pack(fill="x")
        self.button(row, "选择音频文件", self.add_files, accent=True, side="left")
        self.button(row, "导入文件夹", self.add_folder, side="left", padx=8)
        ttk.Label(files, text="支持 WAV、MP3、M4A、FLAC、OGG、AAC 等 · 每个文件独立分析 · 单条失败不中断批量任务", style="Muted.TLabel").pack(anchor="w", pady=(12, 0))
        self.text_input = scrolledtext.ScrolledText(text, height=3, wrap="word", relief="flat", font=("Microsoft YaHei UI", 10))
        self.text_input.pack(side="left", fill="both", expand=True, padx=(0, 12))
        self.button(text, "加入队列", self.add_text, accent=True, side="right")
        toolbar = ttk.Frame(main)
        toolbar.grid(row=4, column=0, sticky="ew", pady=(18, 10))
        ttk.Label(toolbar, text="2  处理队列", style="Heading.TLabel").pack(side="left")
        self.counts = ttk.Label(toolbar, text="暂无任务", style="Muted.TLabel")
        self.counts.pack(side="left", padx=15)
        self.button(toolbar, "移除选中", self.remove_selected, side="right")
        self.button(toolbar, "重试异常", self.retry_failed, side="right", padx=6)
        panes = ttk.Panedwindow(main, orient="horizontal", height=220)
        panes.grid(row=5, column=0, sticky="nsew")
        left, right = ttk.Frame(panes), ttk.Frame(panes, padding=(14, 0, 0, 0))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(2, weight=1)
        panes.add(left, weight=5)
        panes.add(right, weight=6)
        tree_frame = ttk.Frame(left)
        tree_frame.grid(row=0, column=0, sticky="nsew")
        self.tree = ttk.Treeview(tree_frame, columns=("status", "type", "time"), selectmode="extended", height=5)
        for name, title, width in (("#0", "来源 / 文件名", 190), ("status", "状态", 70), ("type", "分类", 85), ("time", "耗时", 65)):
            self.tree.heading(name, text=title)
            self.tree.column(name, width=width, minwidth=55, stretch=name == "#0")
        scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.tag_configure("完成", foreground="#087F5B")
        self.tree.tag_configure("失败", foreground="#C83D3D")
        self.tree.tag_configure("降级完成", foreground="#A56600")
        self.tree.bind("<<TreeviewSelect>>", self.show_selected)
        row = ttk.Frame(left)
        row.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        self.start_button = self.button(row, "开始处理待办", self.start_batch, accent=True, side="left")
        self.batch_stop_button = self.button(row, "完成当前后停止", self.request_stop, idle=False, side="left", padx=8)
        ttk.Label(right, text="3  结果详情", style="Heading.TLabel").grid(row=0, column=0, sticky="w")
        self.selected_label = ttk.Label(right, text="在左侧选择任务查看结果", style="Muted.TLabel", wraplength=460)
        self.selected_label.grid(row=1, column=0, sticky="w", pady=(6, 8))
        self.result_tabs = ttk.Notebook(right)
        self.result_tabs.grid(row=2, column=0, sticky="nsew")
        self.outputs = {}
        for key, label in (("result", "处理结果"), ("transcript", "识别文字"), ("reasoning", "检索 / Agent"), ("raw", "结构化结果")):
            widget = scrolledtext.ScrolledText(self.result_tabs, wrap="word", relief="flat", padx=12, pady=12,
                width=35, height=5, font=("Microsoft YaHei UI", 10), foreground=INK, background=PANEL, state="disabled")
            self.result_tabs.add(widget, text=label)
            self.outputs[key] = widget
        row = ttk.Frame(right)
        row.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        self.speech_mode = tk.StringVar(value="AI 回复")
        ttk.Combobox(row, textvariable=self.speech_mode, values=["AI 回复", "完整摘要"], state="readonly", width=10).pack(side="left")
        self.speak_button = ttk.Button(row, text="朗读选中结果", command=self.speak_selected)
        self.speak_button.pack(side="left", padx=6)
        self.speech_stop_button = ttk.Button(row, text="停止朗读", command=self.stop_speech.set)
        self.speech_stop_button.pack(side="left")
        row = ttk.Frame(main)
        row.grid(row=6, column=0, sticky="ew", pady=(12, 8))
        self.auto_speak = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="整批完成后朗读选中结果", variable=self.auto_speak).pack(side="left")
        self.button(row, "导出全部结果", self.export_results, side="right")
        self.button(row, "复制当前页", self.copy_current, idle=False, side="right", padx=6)
        self.progress = ttk.Progressbar(main, mode="indeterminate")
        self.progress.grid(row=7, column=0, sticky="ew")
        self.status = ttk.Label(main, text="就绪 · 选择输入方式开始", style="Muted.TLabel", wraplength=950)
        self.status.grid(row=8, column=0, sticky="w", pady=(8, 0))
        self.update_provider()
        self.update_owner_summary()

    def update_provider(self):
        state = "密钥已配置" if self.settings.api_key else "请设置密钥"
        self.provider_label.configure(text=f"{PROVIDERS[self.settings.provider]}\n{self.settings.model} · {state}\n{self.settings.stt_engine} / {self.settings.stt_model}")

    def idle(self):
        return not (self.processing or self.recording or self.playing or self.utility_busy or self.closing)

    def refresh_controls(self):
        enabled = self.idle()
        for widget in self.idle_controls:
            widget.configure(state="normal" if enabled else "disabled")
        for widget in self.idle_combos:
            widget.configure(state="readonly" if enabled else "disabled")
        self.text_input.configure(state="normal" if enabled else "disabled")
        self.record_stop_button.configure(state="normal" if self.recording else "disabled")
        self.batch_stop_button.configure(state="normal" if self.processing and not self.stop_batch.is_set() else "disabled")
        self.speak_button.configure(state="normal" if enabled else "disabled")
        self.speech_stop_button.configure(state="normal" if self.playing else "disabled")

    def post(self, kind, payload=None):
        self.events.put((kind, payload))

    def worker(self, target):
        self.workers = [thread for thread in self.workers if thread.is_alive()]
        thread = threading.Thread(target=target, daemon=True)
        self.workers.append(thread)
        thread.start()

    def open_settings(self):
        if self.idle():
            SettingsDialog(self)

    def open_context(self):
        if not self.idle():
            return
        from src.desktop.context_dialog import ContextDialog
        try:
            ContextDialog(self)
        except (OSError, ValueError, TypeError) as error:
            messagebox.showerror("机主设置读取失败", safe_error(error), parent=self.root)

    def update_owner_summary(self):
        from src.desktop.context_dialog import owner_summary
        from src.context.stores import OwnerSettingsStore
        try:
            self.owner_label.configure(text=owner_summary(OwnerSettingsStore(root=self.store.root).load()))
        except (OSError, ValueError, TypeError) as error:
            self.owner_label.configure(text="机主设置读取失败\n请打开管理页检查。")
            self.status.configure(text="机主设置读取失败：" + safe_error(error))
        self._owner_refresh_at = time.monotonic() + 15

    def context_changed(self):
        """Discard only an idle facade so contact/state edits apply next time."""
        if not self.idle():
            raise RuntimeError("任务运行期间不能修改机主与号码设置，请等待任务结束。")
        self.service.close()
        self.update_owner_summary()
        self.status.configure(text="机主与号码设置已更新 · 下条来电使用新设置；评测仍使用隔离状态")

    def apply_settings(self, settings):
        self.service.close()
        self.settings = settings
        self.service = DesktopService(settings, root=self.store.root)
        self.download_allowed = False
        self.update_provider()
        self.status.configure(text="设置已应用 · 新任务将使用新的密钥与语音设置")

    def add_job(self, job):
        self.jobs[job.id] = job
        self.tree.insert("", "end", iid=job.id, text=Path(job.source).name, values=(job.status, "—", "—"))
        self.tree.selection_set(job.id)
        self.tree.see(job.id)
        self.update_counts()

    def add_audio(self, paths):
        existing = {str(Path(job.source).resolve()).casefold() for job in self.jobs.values() if job.kind == "audio"}
        count = 0
        for path in collect_audio(paths):
            if path.casefold() not in existing:
                self.add_job(Job(source=path, caller_number=self.caller.get().strip()))
                existing.add(path.casefold())
                count += 1
        self.status.configure(text=f"已加入 {count} 个音频文件 · 点击“开始处理待办”")

    def add_files(self):
        paths = filedialog.askopenfilenames(parent=self.root, title="选择一个或多个录音文件",
            filetypes=[("音频文件", " ".join("*" + ext for ext in sorted(AUDIO_EXTENSIONS))), ("所有文件", "*.*")])
        if paths:
            self.add_audio(paths)

    def add_folder(self):
        path = filedialog.askdirectory(parent=self.root, title="选择录音文件夹（不递归子目录）")
        if path:
            try:
                self.add_audio([path])
            except OSError as error:
                messagebox.showerror("导入失败", safe_error(error), parent=self.root)

    def add_text(self):
        text = self.text_input.get("1.0", "end").strip()
        if text:
            self.add_job(Job(source=f"文字输入 {len(self.jobs) + 1}", kind="text", text=text,
                             caller_number=self.caller.get().strip()))
            self.text_input.delete("1.0", "end")
        else:
            messagebox.showinfo("尚未输入", "请输入需要分析的来电文字。", parent=self.root)

    def remove_selected(self):
        for job_id in self.tree.selection():
            self.jobs.pop(job_id, None)
            self.tree.delete(job_id)
        self.update_counts()
        self.show_selected()

    def retry_failed(self):
        for job in self.jobs.values():
            if job.status in ("失败", "降级完成"):
                job.status, job.error = "待处理", ""
                self.update_job(job.id)
        self.start_batch()

    def start_batch(self):
        if not self.idle():
            return
        pending = [job for job in self.jobs.values() if job.status == "待处理"]
        if not pending:
            messagebox.showinfo("没有待办", "请先添加录音文件或文字。失败或降级的任务可点击“重试异常”。", parent=self.root)
            return
        if not self.settings.api_key:
            self.open_settings()
            return
        if any(job.kind == "audio" for job in pending) and not self.service.transcriber.is_cached() and not self.download_allowed:
            yes = messagebox.askyesno("首次下载识别模型", f"本机尚未缓存 {self.settings.stt_engine} / {self.settings.stt_model}。\n\n首次识别会下载模型，按型号约需数百 MB 至数 GB；可在设置中改选 tiny / base / small。\n下载与模型加载期间，停止队列需等待当前任务完成。\n\n是否允许本次下载？", parent=self.root)
            if not yes:
                return
            self.download_allowed = True
        self.processing = True
        self.update_owner_summary()
        self.stop_batch.clear()
        self.progress.start(12)
        self.status.configure(text="正在准备处理队列……")
        self.refresh_controls()
        self.worker(lambda: run_batch(pending, self.service, self.stop_batch, self.post,
            self.download_allowed, self.store.root / "data" / "desktop" / "results"))

    def request_stop(self):
        self.stop_batch.set()
        self.status.configure(text="已请求停止：当前识别 / API 请求完成后停止，其余文件保留待处理。")
        self.refresh_controls()

    def refresh_devices(self):
        self.utility_busy = True
        self.refresh_controls()
        self.status.configure(text="正在查找麦克风设备……")
        def task():
            try:
                self.post("devices", MicrophoneRecorder.devices())
            except Exception as error:
                self.post("device_error", safe_error(error))
        self.worker(task)

    def start_recording(self):
        self.recording = True
        self.stop_record.clear()
        self.refresh_controls()
        self.status.configure(text="正在录音 · 对着麦克风说话，然后点击“停止并加入队列”")
        device = self.devices.get(self.device.get())
        self._record_caller = self.caller.get().strip()
        def task():
            try:
                path = MicrophoneRecorder().record(self.stop_record, lambda elapsed, level: self.post("level", (elapsed, level)), device)
                self.post("recorded", path)
            except Exception as error:
                self.post("record_error", safe_error(error))
        self.worker(task)

    def selected(self):
        selected = self.tree.selection()
        return self.jobs.get(selected[0]) if selected else None

    def set_output(self, name, text):
        box = self.outputs[name]
        box.configure(state="normal")
        box.delete("1.0", "end")
        box.insert("1.0", text)
        box.configure(state="disabled")

    def show_selected(self, _event=None):
        job = self.selected()
        self.selected_label.configure(text=job.source if job else "在左侧选择任务查看结果")
        self.set_output("transcript", (job.transcript or job.text or "等待识别……") if job else "")
        self.set_output("result", ("处理失败\n\n" + job.error if job.error else readable_result(job.result)) if job else readable_result({}))
        self.set_output("raw", json.dumps(job.result, ensure_ascii=False, indent=2) if job and job.result else "")
        self.set_output("reasoning", reasoning_details(job.result if job else {}))

    def update_counts(self):
        counts = {status: sum(job.status == status for job in self.jobs.values()) for status in ("完成", "降级完成", "失败", "待处理")}
        self.counts.configure(text=f"共 {len(self.jobs)}  ·  完成 {counts['完成']}  ·  降级 {counts['降级完成']}  ·  失败 {counts['失败']}  ·  待办 {counts['待处理']}")

    def update_job(self, job_id):
        job = self.jobs[job_id]
        self.tree.item(job_id, values=(job.status, job.result.get("call_type_name", "—"), f"{job.elapsed:.1f}s" if job.elapsed else "—"), tags=(job.status,))
        if job_id in self.tree.selection():
            self.show_selected()
        self.update_counts()

    def speak_selected(self):
        if not self.idle():
            return
        job = self.selected()
        if not job or not job.result:
            messagebox.showinfo("暂无结果", "请先选择一条已完成的任务。", parent=self.root)
            return
        text = speech_text(job.result, full=self.speech_mode.get() == "完整摘要")
        voice = self.settings.voice
        self.playing = True
        self.stop_speech.clear()
        self.refresh_controls()
        self.status.configure(text="正在合成 / 播放语音，可点击“停止朗读”")
        def task():
            try:
                AudioSpeaker().speak(text, voice, self.stop_speech)
                self.post("speech_done", None)
            except Exception as error:
                self.post("speech_done", safe_error(error, self.settings.api_key))
        self.worker(task)

    def copy_current(self):
        tab = self.result_tabs.index("current")
        content = self.outputs[("result", "transcript", "reasoning", "raw")[tab]].get("1.0", "end").strip()
        self.root.clipboard_clear()
        self.root.clipboard_append(content)
        self.status.configure(text="当前页内容已复制")

    def export_results(self):
        if not self.jobs:
            return
        path = filedialog.asksaveasfilename(parent=self.root, title="导出全部任务", defaultextension=".json",
            initialfile="言犀处理结果.json", filetypes=[("JSON 结果", "*.json")])
        if path:
            try:
                Path(path).write_text(json.dumps([job.export() for job in self.jobs.values()], ensure_ascii=False, indent=2), encoding="utf-8")
                self.status.configure(text=f"已导出 {len(self.jobs)} 条任务结果")
            except OSError as error:
                messagebox.showerror("导出失败", safe_error(error), parent=self.root)

    def poll(self):
        for _ in range(100):
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "job":
                self.update_job(payload)
            elif kind == "progress":
                job_id, message, current, total = payload
                if not self.stop_batch.is_set():
                    self.status.configure(text=f"[{current}/{total}] {Path(self.jobs[job_id].source).name} · {message}")
                if job_id in self.tree.selection():
                    self.show_selected()
            elif kind == "batch_done":
                self.processing = False
                self.progress.stop()
                abnormal = sum(job.status in ("失败", "降级完成") for job in self.jobs.values())
                self.status.configure(text="队列已暂停，剩余任务可继续处理。" if payload else f"队列处理结束 · {abnormal} 条失败或降级，可查看原因或重试异常。")
                self.refresh_controls()
                self.update_owner_summary()
                if not payload and self.auto_speak.get() and not self.closing and self.selected() and self.selected().result:
                    self.speak_selected()
            elif kind in ("devices", "device_error"):
                self.utility_busy = False
                if kind == "devices":
                    self.devices = {"系统默认麦克风": None, **{label: index for index, label in payload}}
                    self.device_combo.configure(values=list(self.devices))
                    if self.device.get() not in self.devices:
                        self.device.set("系统默认麦克风")
                    self.status.configure(text=f"已检测到 {len(payload)} 个输入设备")
                else:
                    self.status.configure(text="设备读取失败：" + payload)
                self.refresh_controls()
            elif kind == "level":
                elapsed, level = payload
                self.mic_time.configure(text=f"{int(elapsed) // 60:02d}:{int(elapsed) % 60:02d}")
                self.mic_meter.configure(value=level)
            elif kind in ("recorded", "record_error"):
                self.recording = False
                self.mic_meter.configure(value=0)
                self.refresh_controls()
                if kind == "recorded":
                    self.add_job(Job(source=payload, caller_number=self._record_caller))
                    self.status.configure(text="录音已保存并加入队列")
                    if self.auto_process_mic.get() and not self.closing:
                        self.start_batch()
                else:
                    self.status.configure(text="录音失败：" + payload)
                    if not self.closing:
                        messagebox.showerror("录音失败", payload + "\n\n请检查 Windows 麦克风权限和输入设备。", parent=self.root)
            elif kind == "speech_done":
                self.playing = False
                self.status.configure(text="播报失败：" + payload if payload else "语音播报已结束")
                self.refresh_controls()
            elif kind == "notice":
                self.status.configure(text=payload)
        if not self.closing and time.monotonic() >= self._owner_refresh_at:
            self.update_owner_summary()
        self.root.after(80, self.poll)

    def help(self):
        messagebox.showinfo("使用说明", "1. 首次使用填写 Qwen API Key，默认保存在本机 .env，可随时修改。\n2. 在“机主与号码管理”设置空闲、忙碌、免打扰或驾车状态及有效时长。\n3. 可登记联系人、黑白名单、人工信任参考分，查看来电记录。信任分不代表身份认证。\n4. 选择麦克风、音频文件（可批量）或文字输入，再开始处理。\n5. 结果页显示回复、转写、知识命中和各 Agent 的结构化判断，可导出或朗读。\n\n默认不授权放置配送物或替机主更改订单；只能在管理页明确授权。\nRAG 嵌入和对话均调用同一 Qwen Key，均会消耗额度；知识与转写会发送到 Qwen。\n评测台不读取日常机主设置，案例、号码画像及日志独立隔离。\n停止队列会等待当前任务结束。录音在 data/desktop/recordings，结果在 data/desktop/results。\n语音本地识别；播报文字发送到 Edge TTS。请勿分享 .env 或个人运行数据。\n本程序是来电分析原型，不会真实接听、拒接、拨打电话或付款。", parent=self.root)

    def close(self):
        if self.closing:
            return
        if not self.idle():
            if not messagebox.askyesno("退出程序", "仍有任务运行。是否停止录音/播报、等待当前分析完成后退出？", parent=self.root):
                return
        self.closing = True
        self.stop_batch.set()
        self.stop_record.set()
        self.stop_speech.set()
        self.refresh_controls()
        self.status.configure(text="正在释放资源；当前识别 / 网络请求结束后退出……")
        self.finish_close()

    def finish_close(self):
        if any(thread.is_alive() for thread in self.workers):
            self.root.after(150, self.finish_close)
            return
        self.service.close()
        self.root.destroy()


def main():
    root = tk.Tk()
    DesktopApp(root)
    root.mainloop()
