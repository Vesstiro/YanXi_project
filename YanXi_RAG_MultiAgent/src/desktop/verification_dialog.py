"""Separate owner/caller screens for local pre-ring simulation.

Never attach verification state to DesktopService, Job, or result exports.
This is a single-computer role simulation, not caller/owner authentication.
"""
import tkinter as tk
from tkinter import messagebox, ttk

from src.verification.core import (CATEGORIES, QUESTION_SPECS, VerificationController,
    VerificationError, VerificationSession, VerificationVault, default_profile)


class LocalDialog(tk.Toplevel):
    def __init__(self, app, title, width=900, height=710):
        super().__init__(app.root)
        self.app = app
        self.title(title)
        width = min(width, self.winfo_screenwidth() - 80)
        height = min(height, self.winfo_screenheight() - 100)
        x = max(0, (self.winfo_screenwidth() - width) // 2)
        y = max(0, (self.winfo_screenheight() - height) // 2)
        self.geometry(f"{width}x{height}+{x}+{y}")
        self.minsize(min(width, 640), min(height, 480))
        self.transient(app.root)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.app.utility_busy = True
        self.app.refresh_controls()
        self.grab_set()

    def destroy(self):
        if not self.winfo_exists():
            return
        try:
            self.grab_release()
        finally:
            super().destroy()
            self.app.utility_busy = False
            if self.app.root.winfo_exists():
                self.app.refresh_controls()


class VerificationOwnerDialog(LocalDialog):
    def __init__(self, app):
        # Load before creating a modal, so corrupt files do not leave an empty grab.
        self.vault = VerificationVault(app.store.root)
        profile = self.vault.load() or default_profile()
        super().__init__(app, "言犀 · 机主核验资料与题数", 1000, 760)
        self.answers = {}
        self.enabled = {}
        self.entries = []
        self.count = tk.IntVar(value=profile["question_count"])
        self.category = tk.StringVar(value=profile["category"])
        self.until = tk.StringVar(value=profile["valid_until"])
        self.custom = tk.StringVar(value=profile["custom_question"])
        shell = ttk.Frame(self, padding=18)
        shell.pack(fill="both", expand=True)
        ttk.Label(shell, text="机主设置 · 混合题库", style="Heading.TLabel").pack(anchor="w")
        ttk.Label(shell, text="只勾选允许用于核验的资料。原始回答不保存；标准答案仅在本机加密保存，不进入 Qwen、历史或导出。",
                  wraplength=900, style="Muted.TLabel").pack(anchor="w", pady=(6, 10))
        choices = ttk.Frame(shell)
        choices.pack(fill="x")
        ttk.Label(choices, text="每次核验问题数：").pack(side="left")
        for count in (3, 5, 7):
            ttk.Radiobutton(choices, text=f"{count} 题", variable=self.count, value=count).pack(side="left", padx=8)
        # Scroll only the form; save/delete buttons remain reachable.
        holder = ttk.Frame(shell)
        holder.pack(fill="both", expand=True, pady=12)
        canvas = tk.Canvas(holder, highlightthickness=0, background="#F2F5FA")
        bar = ttk.Scrollbar(holder, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        page = ttk.Frame(canvas, padding=(0, 0, 12, 10))
        window = canvas.create_window((0, 0), window=page, anchor="nw")
        page.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))
        def wheel(event):
            canvas.yview_scroll(int(-event.delta / 120), "units")
        canvas.bind("<MouseWheel>", wheel)
        page.columnconfigure(1, weight=1)
        ttk.Label(page, text="当前事项类别").grid(row=0, column=0, sticky="w", pady=6)
        ttk.Combobox(page, textvariable=self.category, values=CATEGORIES, state="readonly").grid(row=0, column=1, sticky="ew", pady=6)
        ttk.Label(page, text="事项有效期（选填）").grid(row=1, column=0, sticky="w", pady=6)
        ttk.Entry(page, textvariable=self.until).grid(row=1, column=1, sticky="ew", pady=6)
        ttk.Label(page, text="格式 YYYY-MM-DD；当天有效。前 3 项为通用资料，后 4 项只用于类别匹配且未过期的当前事项。",
                  wraplength=820, style="Muted.TLabel").grid(row=2, column=0, columnspan=2, sticky="w", pady=8)
        for index, (key, label, _prompt, scope) in enumerate(QUESTION_SPECS):
            row = 3 + index * 2
            data = profile["fields"][key]
            self.enabled[key] = tk.BooleanVar(value=data["enabled"])
            self.answers[key] = tk.StringVar(value=" | ".join(data["answers"]))
            ttk.Checkbutton(page, text=label, variable=self.enabled[key]).grid(row=row, column=0, sticky="w", padx=(0, 15), pady=(9, 2))
            entry = ttk.Entry(page, textvariable=self.answers[key], show="●")
            entry.grid(row=row, column=1, sticky="ew", pady=(9, 2))
            self.entries.append(entry)
            prompt = profile["custom_question"] if key == "custom" else _prompt
            ttk.Label(page, text="提问：" + prompt, wraplength=810, style="Muted.TLabel").grid(row=row+1, column=0, columnspan=2, sticky="w", pady=(0, 3))
        ttk.Label(page, text="自定义问题内容").grid(row=17, column=0, sticky="w", pady=(14, 6))
        ttk.Entry(page, textvariable=self.custom).grid(row=17, column=1, sticky="ew", pady=(14, 6))
        self.show = tk.BooleanVar(value=False)
        ttk.Checkbutton(page, text="机主查看答案", variable=self.show, command=self.toggle_answers).grid(row=18, column=1, sticky="w", pady=8)
        ttk.Label(page, text="别称用 | 分隔，最多 8 个；仅按文字规范化及预设别称匹配，不做语义推断。日期请填写来电者能准确复述的格式。\n"
                  "不要填写身份证号、密码、验证码或完整住址。自定义问题会对来电者显示，不要把答案写进问题里。\n"
                  "本版维护一个当前事项。其他类别或事项过期时若有效题数不足，会显示“无法核验”，不会凑题或认定诈骗。",
                  wraplength=820, style="Muted.TLabel").grid(row=19, column=0, columnspan=2, sticky="w", pady=8)
        # Tk does not bubble wheel events through the form's embedded children.
        def bind_wheel(widget):
            widget.bind("<MouseWheel>", wheel, add=True)
            for child in widget.winfo_children():
                bind_wheel(child)
        bind_wheel(page)
        ttk.Label(shell, text="资料由当前 Windows 账户的 DPAPI 加密；同账户程序仍可能读取。此功能为本地文字模拟，不是 MPC。",
                  wraplength=900, style="Muted.TLabel").pack(anchor="w", pady=(0, 8))
        footer = ttk.Frame(shell)
        footer.pack(fill="x")
        self.delete_button = ttk.Button(footer, text="删除本机核验资料", command=self.delete_profile)
        self.delete_button.pack(side="left")
        ttk.Button(footer, text="取消", command=self.destroy).pack(side="right")
        self.save_button = ttk.Button(footer, text="保存并关闭", command=self.save_profile, style="Accent.TButton")
        self.save_button.pack(side="right", padx=10)

    def toggle_answers(self):
        for entry in self.entries:
            entry.configure(show="" if self.show.get() else "●")

    def snapshot(self):
        return {"question_count": self.count.get(), "category": self.category.get(),
                "valid_until": self.until.get().strip(), "custom_question": self.custom.get().strip(),
                "fields": {key: {"enabled": self.enabled[key].get(),
                                 "answers": [a.strip() for a in value.get().split("|") if a.strip()]}
                           for key, value in self.answers.items()}}

    def save_profile(self):
        try:
            self.vault.save(self.snapshot())
        except VerificationError as error:
            messagebox.showerror("无法保存", str(error), parent=self)
            return
        except (ValueError, OSError, tk.TclError):
            # Never echo Tk input or third-party exception text into dialogs/logs.
            messagebox.showerror("无法保存", "请检查题数、授权勾选、答案数量和有效期。每道授权题需填写答案；资料不足时请补充或降低题数。\n"
                                 "若现有文件损坏或由其他 Windows 账户创建，请先备份后删除重建。", parent=self)
            return
        self.app.status.configure(text="核验资料已在本机加密保存 · 可点击“模拟响铃前核验”测试，无需 API Key")
        self.destroy()

    def delete_profile(self):
        if messagebox.askyesno("删除核验资料", "删除本机核验资料？下次使用需重新填写。不会删除联系人或来电记录。", parent=self):
            try:
                self.vault.delete()
            except OSError:
                messagebox.showerror("删除失败", "无法删除本机核验资料，请检查文件权限。", parent=self)
                return
            self.app.status.configure(text="本机核验资料已删除")
            self.destroy()

    def destroy(self):
        for value in self.answers.values():
            value.set("")
        self.custom.set("")
        super().destroy()


class PreRingDialog(LocalDialog):
    def __init__(self, app):
        super().__init__(app, "言犀 · 来电者视角（文字模拟）", 820, 600)
        self.session = None
        self._timer = None
        self._finished = False
        self.number = tk.StringVar()
        self.category = tk.StringVar(value=CATEGORIES[0])
        self.answer = tk.StringVar()
        self.shell = ttk.Frame(self, padding=24)
        self.shell.pack(fill="both", expand=True)
        ttk.Label(self.shell, text="陌生来电 · 响铃前问答", style="Heading.TLabel").pack(anchor="w")
        ttk.Label(self.shell, text="本页模拟来电者。问答在本地进行，不联网、不录音；不向来电者展示标准答案或逐题对错。\n"
                  "完成、超时或提前结束后才切换为机主的模拟响铃提示。模拟界面的角色切换不提供访问身份认证。",
                  wraplength=740, style="Muted.TLabel").pack(anchor="w", pady=(10, 18))
        self.setup = ttk.Frame(self.shell)
        self.setup.pack(fill="x")
        self.setup.columnconfigure(1, weight=1)
        ttk.Label(self.setup, text="模拟来电号码（可空）").grid(row=0, column=0, sticky="w", padx=(0, 15), pady=8)
        ttk.Entry(self.setup, textvariable=self.number).grid(row=0, column=1, sticky="ew", pady=8)
        ttk.Label(self.setup, text="您这次来电属于哪类事项？").grid(row=1, column=0, sticky="w", padx=(0, 15), pady=8)
        ttk.Combobox(self.setup, textvariable=self.category, values=CATEGORIES, state="readonly").grid(row=1, column=1, sticky="ew", pady=8)
        self.begin_button = ttk.Button(self.setup, text="开始接听前核验", command=self.begin, style="Accent.TButton")
        self.begin_button.grid(row=2, column=1, sticky="e", pady=16)
        self.progress = ttk.Label(self.shell, text="尚未开始 · 不响铃", style="Muted.TLabel")
        self.progress.pack(anchor="w", pady=12)
        self.question = ttk.Label(self.shell, text="", wraplength=730, font=("Microsoft YaHei UI", 14))
        self.question.pack(anchor="w", pady=10)
        self.entry = ttk.Entry(self.shell, textvariable=self.answer)
        self.entry.bind("<Return>", lambda _e: self.submit())
        self.actions = ttk.Frame(self.shell)
        self.submit_button = ttk.Button(self.actions, text="提交，继续下一题", command=self.submit, style="Accent.TButton")
        self.submit_button.pack(side="left")
        ttk.Button(self.actions, text="不知道 / 跳过", command=lambda: self.submit(True)).pack(side="left", padx=10)
        ttk.Button(self.actions, text="提前结束，提醒机主", command=self.cancel_call).pack(side="right")
        self.protocol("WM_DELETE_WINDOW", self.cancel_call)

    def begin(self):
        try:
            result = VerificationController(self.app.store.root).start(self.number.get(), self.category.get())
        except (ValueError, OSError):
            messagebox.showerror("暂无法开始", "请先由机主保存核验资料，并检查号码格式和本机资料文件。此阶段不会调用云端服务。", parent=self)
            return
        self.setup.pack_forget()
        if not isinstance(result, VerificationSession):
            self.finish(result)
            return
        self.session = result
        if self.session.done:
            self.finish(self.session.summary())
            return
        self.entry.pack(fill="x", pady=10)
        self.actions.pack(fill="x", pady=16)
        self.advance()
        self.tick()

    def advance(self):
        self.answer.set("")
        if self.session.done:
            self.finish(self.session.summary())
            return
        answered, total = self.session.progress
        self.progress.configure(text=f"第 {answered+1} / {total} 题 · 每题最多等待 120 秒 · 尚未提醒机主")
        self.question.configure(text=self.session.current_prompt + "\n（请填写简短名称或约定格式；不知道可跳过。）")
        self.entry.focus_set()

    def submit(self, skipped=False):
        if self.session is None or self._finished:
            return
        try:
            self.session.submit("" if skipped else self.answer.get())
        except VerificationError:
            self.answer.set("")
            if self.session.done:
                self.finish(self.session.summary())
            else:
                messagebox.showwarning("无法提交", "请将回答控制在 500 字以内。", parent=self)
            return
        self.advance()

    def tick(self):
        self._timer = None
        if self._finished:
            return
        if self.session.done:
            self.finish(self.session.summary())
        else:
            self._timer = self.after(500, self.tick)

    def cancel_call(self):
        if self.session is not None and not self._finished:
            self.finish(self.session.finish())
        else:
            self.destroy()

    def finish(self, summary):
        if self._finished:
            return
        self._finished = True
        app = self.app
        self.destroy()
        RingSummaryDialog(app, summary)

    def destroy(self):
        if self._timer is not None:
            self.after_cancel(self._timer)
            self._timer = None
        if self.session is not None and not self.session.done:
            self.session.finish()
        self.session = None
        self.answer.set("")
        self.number.set("")
        super().destroy()


class SoftCard(tk.Canvas):
    """Native rounded card that follows its contents without image assets."""
    def __init__(self, parent, *, fill="white", border="#E2E8F1", padding=18, **kwargs):
        super().__init__(parent, bg="#F5F7FB", highlightthickness=0, bd=0, **kwargs)
        self.fill, self.border, self.padding = fill, border, padding
        self.content = tk.Frame(self, bg=fill)
        self._window = self.create_window(padding, padding, anchor="nw", window=self.content)
        self.bind("<Configure>", self._layout)
        self.content.bind("<Configure>", self._fit)

    def _fit(self, _event=None):
        height = self.content.winfo_reqheight() + self.padding * 2
        if int(self.cget("height")) != height:
            self.configure(height=height)
        self._outline()

    def _layout(self, event):
        self.itemconfigure(self._window, width=max(1, event.width - self.padding * 2))
        self._outline()

    def _outline(self):
        width, height, radius = self.winfo_width()-2, int(self.cget("height"))-2, 18
        self.delete("border")
        self.create_polygon(1+radius, 1, width-radius, 1, width, 1, width, 1+radius,
                            width, height-radius, width, height, width-radius, height,
                            1+radius, height, 1, height, 1, height-radius, 1, 1+radius, 1, 1,
                            smooth=True, splinesteps=24, fill=self.fill, outline=self.border,
                            width=1, tags="border")
        self.tag_lower("border")


def draw_mood(parent, mood, color, background, size=64):
    """Small vector illustrations avoid emoji font/download dependencies."""
    icon = tk.Canvas(parent, width=size, height=size, bg=background, highlightthickness=0)
    icon.create_oval(3, 3, size-3, size-3, fill=background, outline=color, width=2)
    center = size/2
    if mood == "alert":
        icon.create_line(center, 16, center, 35, fill=color, width=4, capstyle="round")
        icon.create_oval(center-2, 43, center+2, 47, fill=color, outline=color)
    elif mood == "smile":
        for x in (center-10, center+10):
            icon.create_oval(x-2, 22, x+2, 26, fill=color, outline=color)
        icon.create_arc(18, 23, size-18, size-16, start=200, extent=140,
                        style="arc", outline=color, width=3)
    elif mood == "person":
        icon.create_oval(center-7, 16, center+7, 30, outline=color, width=2)
        icon.create_arc(15, 35, size-15, size-5, start=0, extent=180, style="arc", outline=color, width=2)
    else:
        for x in (center-10, center+10):
            icon.create_oval(x-2, 22, x+2, 26, fill=color, outline=color)
        icon.create_line(23, 42, size-23, 42, fill=color, width=3, capstyle="round")
    return icon


class RingSummaryDialog(LocalDialog):
    def __init__(self, app, summary):
        super().__init__(app, "言犀 · 模拟响铃 · 机主提示", 900, 750)
        self.summary = summary
        self.decision = None
        self.text_labels = []
        self.configure(bg="#F5F7FB")
        shell = tk.Frame(self, bg="#F5F7FB", padx=24, pady=20)
        shell.pack(fill="both", expand=True)
        shell.columnconfigure(0, weight=1)
        shell.rowconfigure(1, weight=1)
        header = tk.Frame(shell, bg="#F5F7FB")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        self._label(header, "言犀  /  来电守护", color="#7A889E", size=9).pack(anchor="w")
        title_row = tk.Frame(header, bg="#F5F7FB")
        title_row.pack(fill="x", pady=(6, 0))
        self._label(title_row, "有来电，先看一眼核验结果", size=19, bold=True).pack(side="left")
        self._label(title_row, "  模拟响铃  ", color="#5372BA", size=9,
                    bg="#EAF0FF", padx=9, pady=5).pack(side="right")

        # Only the cards scroll. Decision buttons always stay visible.
        body = tk.Frame(shell, bg="#F5F7FB")
        body.grid(row=1, column=0, sticky="nsew")
        self.cards_canvas = tk.Canvas(body, bg="#F5F7FB", highlightthickness=0)
        scroll = ttk.Scrollbar(body, orient="vertical", command=self.cards_canvas.yview)
        self.cards_canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y", padx=(8, 0))
        self.cards_canvas.pack(fill="both", expand=True)
        cards = tk.Frame(self.cards_canvas, bg="#F5F7FB")
        cards.columnconfigure(0, weight=1)
        window = self.cards_canvas.create_window(0, 0, anchor="nw", window=cards)
        cards.bind("<Configure>", lambda _e: self.cards_canvas.configure(scrollregion=self.cards_canvas.bbox("all")))
        self.cards_canvas.bind("<Configure>", lambda e: self.cards_canvas.itemconfigure(window, width=e.width))

        mood, color, tint, heading, detail = self._appearance(summary)
        self.mood = mood
        hero = SoftCard(cards, fill=tint, border=tint, padding=18)
        hero.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        draw_mood(hero.content, mood, color, tint).pack(side="left", padx=(0, 16))
        hero_words = tk.Frame(hero.content, bg=tint)
        hero_words.pack(side="left", fill="both", expand=True)
        self._label(hero_words, heading, color=color, size=15, bold=True, wrap=True).pack(anchor="w", pady=(2, 5))
        self._label(hero_words, detail, color="#637086", size=10, wrap=True).pack(anchor="w")

        metrics = tk.Frame(cards, bg="#F5F7FB")
        metrics.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        metrics.columnconfigure(0, weight=1, uniform="metric")
        metrics.columnconfigure(1, weight=1, uniform="metric")
        accuracy_card = SoftCard(metrics, padding=18)
        accuracy_card.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self._label(accuracy_card.content, "回答匹配表现", size=11, bold=True).pack(anchor="w")
        gauge_row = tk.Frame(accuracy_card.content, bg="white")
        gauge_row.pack(fill="x", pady=(10, 8))
        gauge = tk.Canvas(gauge_row, width=106, height=106, bg="white", highlightthickness=0)
        gauge.pack(side="left", padx=(0, 12))
        gauge.create_oval(8, 8, 98, 98, outline="#EDF1F7", width=8)
        if summary.accuracy is not None:
            if summary.accuracy == 1:
                gauge.create_oval(8, 8, 98, 98, outline="#6489E8", width=8)
            elif summary.accuracy > 0:
                gauge.create_arc(8, 8, 98, 98, start=90, extent=-360*summary.accuracy,
                                 outline="#6489E8", width=8, style="arc")
        value = "—" if summary.accuracy is None else f"{summary.accuracy:.0%}"
        gauge.create_text(53, 51, text=value, fill="#243B61", font=("Microsoft YaHei UI", 20, "bold"))
        gauge.create_text(53, 72, text="准确率", fill="#8A96A8", font=("Microsoft YaHei UI", 8))
        gauge_words = tk.Frame(gauge_row, bg="white")
        gauge_words.pack(side="left", fill="both", expand=True)
        self._label(gauge_words, value, color="#4567B8", size=17, bold=True).pack(anchor="w", pady=(10, 4))
        self._label(gauge_words, f"匹配 {summary.matched} / 可核对 {summary.checked} 项" if summary.checked else "暂无可核对项",
                    color="#7A889E", size=9, wrap=True).pack(anchor="w")
        self._label(accuracy_card.content, "信息匹配度，不是身份可信概率", color="#8A96A8", size=9, wrap=True).pack(anchor="w")

        coverage_card = SoftCard(metrics, padding=18)
        coverage_card.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        self._label(coverage_card.content, "这次核对了多少", size=11, bold=True).pack(anchor="w")
        coverage_row = tk.Frame(coverage_card.content, bg="white")
        coverage_row.pack(fill="x", pady=(12, 5))
        self._label(coverage_row, f"{summary.coverage:.0%}", size=24, bold=True, color="#385C77").pack(side="left")
        self._label(coverage_row, " 核对覆盖率", color="#8A96A8", size=9).pack(side="left", pady=(10, 0))
        bar = tk.Canvas(coverage_card.content, height=8, bg="white", highlightthickness=0)
        bar.pack(fill="x", pady=(0, 12))
        def draw_bar(event):
            bar.delete("all")
            bar.create_line(4, 4, max(4, event.width-4), 4, fill="#EDF1F7", width=7, capstyle="round")
            if summary.coverage:
                bar.create_line(4, 4, max(4, (event.width-8)*summary.coverage+4), 4,
                                fill="#80B7B0", width=7, capstyle="round")
        bar.bind("<Configure>", draw_bar)
        self._label(coverage_card.content, f"已提交 {summary.answered} / 设置 {summary.question_count} 题",
                    color="#7A889E", size=9).pack(anchor="w")
        counts = tk.Frame(coverage_card.content, bg="white")
        counts.pack(fill="x", pady=(9, 0))
        for text, foreground, background in ((f"匹配 {summary.matched}", "#357C6A", "#EAF5EF"),
                (f"不符 {summary.mismatched}", "#BC5960", "#FCEEEF"),
                (f"未知 {summary.unknown}", "#7A889E", "#F0F3F8")):
            self._label(counts, text, color=foreground, bg=background, size=9, padx=7, pady=4).pack(side="left", padx=(0, 5))

        advice = SoftCard(cards, padding=18)
        advice.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        advice_title = tk.Frame(advice.content, bg="white")
        advice_title.pack(fill="x", pady=(0, 8))
        self._label(advice_title, "接听前的小提醒", size=11, bold=True).pack(side="left")
        self._label(advice_title, " ! ", color="#C65E65", bg="#FCEEEF", size=10, bold=True, padx=4).pack(side="right")
        self._label(advice.content, summary.recommendation, size=10, color="#64748B", wrap=True,
                    justify="left", pady=2).pack(anchor="w", fill="x")
        self._label(cards, "仅展示汇总 · 原始回答不保存 · 标准答案不展示", size=9,
                    color="#8A96A8", wrap=True, pady=3).grid(row=3, column=0, sticky="w")
        def bind_wheel(widget):
            widget.bind("<MouseWheel>", lambda e: self.cards_canvas.yview_scroll(int(-e.delta/120), "units"), add=True)
            for child in widget.winfo_children():
                bind_wheel(child)
        bind_wheel(cards)
        self.cards_canvas.bind("<MouseWheel>", lambda e: self.cards_canvas.yview_scroll(int(-e.delta/120), "units"))

        footer = tk.Frame(shell, bg="#F5F7FB")
        footer.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        self.hint = self._label(footer, "由你决定如何接听。以下操作仅用于模拟。", color="#8A96A8", size=9, wrap=True)
        self.hint.pack(anchor="w", fill="x", pady=(0, 10))
        actions = tk.Frame(footer, bg="#F5F7FB")
        actions.pack(fill="x")
        self.decision_buttons = []
        for label, foreground, background, hover in (("接听", "white", "#5076DC", "#4267C8"),
                ("转留言", "#526888", "#E9EEF8", "#DFE7F5"),
                ("拒接", "#B5636A", "#F9EBED", "#F3DFE2")):
            button = tk.Button(actions, text=label + "（模拟）", command=lambda value=label: self.choose(value),
                               font=("Microsoft YaHei UI", 10), bg=background, fg=foreground,
                               activebackground=hover, activeforeground=foreground, relief="flat", bd=0,
                               padx=19, pady=11, cursor="hand2", highlightthickness=0)
            button.pack(side="left", padx=(0, 10))
            self.decision_buttons.append(button)
        tk.Button(actions, text="关闭", command=self.destroy, bg="#F5F7FB", fg="#7A889E",
                  font=("Microsoft YaHei UI", 10), relief="flat", bd=0, padx=12, pady=11,
                  cursor="hand2", activebackground="#E9EEF8").pack(side="right")
        # The first sound in this flow occurs only after question collection ends.
        self.bell()

    def _label(self, parent, text, *, color="#293B55", bg=None, size=10, bold=False, wrap=False, **kwargs):
        label = tk.Label(parent, text=text, bg=bg or parent.cget("bg"), fg=color, anchor="w",
                         font=("Microsoft YaHei UI", size, "bold" if bold else "normal"), **kwargs)
        if wrap:
            label.configure(wraplength=500, justify="left")
            parent.bind("<Configure>", lambda e: label.configure(wraplength=max(100, e.width-4)), add=True)
        self.text_labels.append(label)
        return label

    @staticmethod
    def _appearance(summary):
        if summary.status == "blacklist":
            return "alert", "#BF6168", "#FBEFF0", "这个号码在你的黑名单中", "本次跳过问答，请按你的名单设置处理。"
        if summary.status == "known":
            return "person", "#587BB5", "#EDF2FC", "这是你登记过的号码", "已跳过陌生来电问答，接听时仍请确认对方身份。"
        if summary.status in ("cancelled", "timeout", "unavailable"):
            return "neutral", "#B28A4C", "#FCF5E8", "信息还不充分，先听听来意", "核验未完成或题库不足，暂不能据此判断。"
        if summary.mismatched:
            return "alert", "#BF6168", "#FBEFF0", "有些信息，需要再确认一下", "发现回答与预留资料不符；这不等于对方存在欺骗。"
        if summary.checked and summary.coverage >= .8:
            return "smile", "#438975", "#EBF5EF", "信息较一致，可以多一份参考", "回答与已核对资料一致，仍不代表身份已经验证。"
        return "neutral", "#B28A4C", "#FCF5E8", "先别着急下结论", "可核对的信息较少，不知道答案也不等于欺骗。"

    def choose(self, value):
        self.decision = value
        for button in self.decision_buttons:
            button.configure(state="disabled")
        self.hint.configure(text=f"已选择：{value}（模拟）。本次问答不保存，不加入云端分析或来电历史。")
