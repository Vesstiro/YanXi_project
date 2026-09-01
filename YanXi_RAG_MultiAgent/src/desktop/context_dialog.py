"""Local owner/contact/history controls. No model or network calls are made here."""
import json
import math
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk

from src.desktop.settings import ROOT, safe_error

PRESENCE_LABELS = {"free": "空闲", "busy": "忙碌", "dnd": "免打扰", "driving": "驾车"}
LIST_LABELS = {"normal": "普通号码", "whitelist": "白名单", "blacklist": "黑名单"}


def owner_summary(owner):
    """Compact, explicit state/authorization summary for the daily workspace."""
    mode = owner.get("presence_mode", "free")
    lines = ["机主：" + PRESENCE_LABELS.get(mode, str(mode))]
    if owner.get("reason"):
        lines.append(str(owner["reason"])[:16])
    expires = owner.get("expires_at")
    if expires:
        try:
            stamp = datetime.fromtimestamp(float(expires))
            lines.append("状态到期：" + stamp.strftime("%m-%d %H:%M"))
        except (TypeError, ValueError, OSError, OverflowError):
            lines.append("状态到期：" + str(expires)[:28])
    lines.append("放置授权：" + (str(owner.get("allowed_dropoff"))[:12] if owner.get("allowed_dropoff") else "未设置"))
    lines.append("等价改单：" + ("已授权" if owner.get("allow_order_changes") else "未授权"))
    return "\n".join(lines)


class ContextController:
    """Small GUI-independent local-storage adapter, also exercised offline."""
    def __init__(self, root=ROOT, *, owner_store=None, caller_store=None, history_store=None,
                 can_edit=None, changed=None):
        from src.context.stores import CallerStore, HistoryStore, OwnerSettingsStore
        self.root = Path(root)
        self.owner_store = owner_store if owner_store is not None else OwnerSettingsStore(root=self.root)
        self.caller_store = caller_store if caller_store is not None else CallerStore(
            self.root / "data" / "profiles" / "caller_profiles.json")
        self.history_store = history_store if history_store is not None else HistoryStore(
            self.root / "data" / "call_logs")
        self.can_edit = can_edit or (lambda: True)
        self.changed = changed or (lambda: None)

    def require_idle(self):
        if not self.can_edit():
            raise RuntimeError("任务运行期间不能修改设置，请等待当前任务结束。")

    def save_owner(self, values):
        self.require_idle()
        # Validation stays in the store so CLI and GUI enforce the same rules.
        saved = self.owner_store.save(dict(values))
        self.changed()
        return saved

    def save_contact(self, number, display_name="", list_status="normal", trust_override="", notes=""):
        self.require_idle()
        if list_status not in LIST_LABELS:
            raise ValueError("请选择普通号码、白名单或黑名单。")
        raw = str(trust_override).strip() if trust_override is not None else ""
        override = None
        if raw:
            try:
                override = float(raw)
            except (TypeError, ValueError):
                raise ValueError("人工信任参考分应为 0～100，留空则使用历史行为评分。") from None
            if not math.isfinite(override) or not 0 <= override <= 100:
                raise ValueError("人工信任参考分必须在 0～100 之间。")
        profile = self.caller_store.set_contact(number, display_name=display_name,
            trust_override=override, list_status=list_status, notes=notes)
        self.changed()
        return profile


class ContextDialog(tk.Toplevel):
    def __init__(self, app):
        # Read before creating the modal window, so IO errors never leave a grab behind.
        self.controller = ContextController(root=app.store.root, can_edit=app.idle, changed=app.context_changed)
        self.owner = self.controller.owner_store.load()
        profiles = self.controller.caller_store.list_profiles()
        history = self.controller.history_store.recent(limit=100)
        super().__init__(app.root)
        self.app = app
        self.title("言犀 · 机主与号码管理")
        width = min(1060, max(760, self.winfo_screenwidth() - 80))
        height = min(760, max(620, self.winfo_screenheight() - 100))
        self.geometry(f"{width}x{height}")
        self.minsize(min(880, width), min(650, height))
        self.resizable(True, True)
        self.transient(app.root)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        shell = ttk.Frame(self, padding=18)
        shell.pack(fill="both", expand=True)
        ttk.Label(shell, text="日常来电的状态、联系人和处理记录", style="Heading.TLabel").pack(anchor="w")
        ttk.Label(shell, text="本页只修改本机日常管家设置；评测案例使用隔离状态。任务运行期间禁止修改。",
            style="Muted.TLabel", wraplength=920).pack(anchor="w", pady=(6, 12))
        self.tabs = ttk.Notebook(shell)
        self.tabs.pack(fill="both", expand=True)
        self.state_page, self.contacts_page, self.history_page = (ttk.Frame(self.tabs, padding=14) for _ in range(3))
        self.tabs.add(self.state_page, text="机主状态与授权")
        self.tabs.add(self.contacts_page, text="联系人 / 信任参考")
        self.tabs.add(self.history_page, text="最近来电记录")
        self._build_state()
        self._build_contacts()
        self._build_history()
        footer = ttk.Frame(shell)
        footer.pack(fill="x", pady=(10, 0))
        self.hint = ttk.Label(footer, text="信任分只反映历史行为或人工设定，不证明来电者身份。", style="Muted.TLabel", wraplength=760)
        self.hint.pack(side="left", fill="x", expand=True)
        ttk.Button(footer, text="关闭", command=self.destroy).pack(side="right", padx=(10, 0))
        self._fill_profiles(profiles)
        self._fill_history(history)
        self.grab_set()

    def _build_state(self):
        # Keep the save button reachable on shorter laptop screens or larger fonts.
        outer = self.state_page
        self.state_canvas = tk.Canvas(outer, highlightthickness=0, background="#F2F5FA")
        scroll = ttk.Scrollbar(outer, orient="vertical", command=self.state_canvas.yview)
        self.state_canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.state_canvas.pack(side="left", fill="both", expand=True)
        page = ttk.Frame(self.state_canvas, padding=(0, 0, 10, 10))
        window = self.state_canvas.create_window((0, 0), window=page, anchor="nw")
        page.bind("<Configure>", lambda _event: self.state_canvas.configure(scrollregion=self.state_canvas.bbox("all")))
        self.state_canvas.bind("<Configure>", lambda event: self.state_canvas.itemconfigure(window, width=event.width))
        self.state_canvas.bind("<MouseWheel>", lambda event: self.state_canvas.yview_scroll(int(-event.delta / 120), "units"))
        page.columnconfigure(1, weight=1)
        self.mode = tk.StringVar(value=PRESENCE_LABELS.get(self.owner.get("presence_mode", "free"), "空闲"))
        self.reason = tk.StringVar(value=self.owner.get("reason", ""))
        self.duration = tk.StringVar(value=str(self.owner.get("duration_minutes", 0)))
        self.dropoff = tk.StringVar(value=self.owner.get("allowed_dropoff", ""))
        self.order_changes = tk.BooleanVar(value=bool(self.owner.get("allow_order_changes", False)))
        self.callback = tk.StringVar(value=self.owner.get("callback_note", ""))
        for row, label, variable in ((0, "机主当前状态", self.mode), (1, "状态原因（选填）", self.reason),
                                    (2, "状态持续分钟数", self.duration), (4, "允许放置的位置", self.dropoff),
                                    (7, "回电说明（选填）", self.callback)):
            ttk.Label(page, text=label).grid(row=row, column=0, sticky="w", padx=(0, 18), pady=7)
            widget = ttk.Combobox(page, textvariable=variable, values=list(PRESENCE_LABELS.values()), state="readonly") if row == 0 else ttk.Entry(page, textvariable=variable)
            widget.grid(row=row, column=1, sticky="ew", pady=7)
        ttk.Label(page, text="可填 0～10080 分钟；0 表示手动解除，到期恢复空闲。时长只作用于状态，不自动撤销下方授权。",
            wraplength=780, style="Muted.TLabel").grid(row=3, column=0, columnspan=2, sticky="w", pady=(0, 10))
        ttk.Label(page, text="默认留空：AI 不会自行承诺“放门口”。只填写你明确同意的配送放置位置。",
            wraplength=780, style="Muted.TLabel").grid(row=5, column=0, columnspan=2, sticky="w", pady=(0, 12))
        ttk.Checkbutton(page, text="授权在不加价、同价类似餐品范围内代为调整订单（默认关闭）",
            variable=self.order_changes).grid(row=6, column=0, columnspan=2, sticky="w", pady=8)
        ttk.Label(page, text="改单授权不包括支付、退款、转账或验证码；涉及金额变动或条件不明确时仍须机主确认。\n回电说明只能填写真实意愿，例如“稍后查看留言”；请勿把号码标签当作身份认证。",
            wraplength=780, style="Muted.TLabel").grid(row=8, column=0, columnspan=2, sticky="w", pady=(5, 12))
        self.state_summary = ttk.Label(page, text=owner_summary(self.owner).replace("\n", "  ·  "), wraplength=800, style="Muted.TLabel")
        self.state_summary.grid(row=9, column=0, columnspan=2, sticky="w", pady=8)
        self.state_save_button = ttk.Button(page, text="保存机主设置", command=self.save_owner, style="Accent.TButton")
        self.state_save_button.grid(row=10, column=1, sticky="e", pady=(6, 0))

    @staticmethod
    def _tree(parent, columns):
        frame = ttk.Frame(parent)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        tree = ttk.Treeview(frame, columns=[item[0] for item in columns], show="headings", selectmode="browse", height=5)
        for name, label, width in columns:
            tree.heading(name, text=label)
            tree.column(name, width=width, minwidth=60, stretch=name in ("summary", "name"))
        vertical = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        horizontal = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        return frame, tree

    def _build_contacts(self):
        page = self.contacts_page
        page.columnconfigure(0, weight=1)
        page.rowconfigure(1, weight=1)
        bar = ttk.Frame(page)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(bar, text="黑名单与白名单互斥；白名单也不能绕过诈骗检查。", style="Muted.TLabel").pack(side="left")
        ttk.Button(bar, text="刷新", command=self.refresh_contacts).pack(side="right")
        frame, self.contacts_tree = self._tree(page, (("number", "号码", 135), ("name", "联系人", 105),
            ("list", "名单", 82), ("score", "参考分", 76), ("history", "历史分", 76), ("count", "次数", 60),
            ("summary", "最近摘要", 225)))
        frame.grid(row=1, column=0, sticky="nsew")
        self.contacts_tree.bind("<<TreeviewSelect>>", self.select_contact)
        form = ttk.LabelFrame(page, text="编辑联系人 / 新增号码", padding=10)
        form.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        form.columnconfigure(1, weight=1)
        form.columnconfigure(3, weight=1)
        self.number, self.display_name, self.override, self.notes = (tk.StringVar() for _ in range(4))
        self.list_status = tk.StringVar(value=LIST_LABELS["normal"])
        for row, column, label, variable in ((0, 0, "来电号码", self.number), (0, 2, "联系人名称", self.display_name),
                                            (1, 0, "名单状态", self.list_status), (1, 2, "人工分（可空）", self.override)):
            ttk.Label(form, text=label).grid(row=row, column=column, sticky="w", padx=(0, 8), pady=5)
            widget = ttk.Combobox(form, textvariable=variable, values=list(LIST_LABELS.values()), state="readonly") if variable is self.list_status else ttk.Entry(form, textvariable=variable)
            widget.grid(row=row, column=column + 1, sticky="ew", padx=(0, 16), pady=5)
        ttk.Label(form, text="备注").grid(row=2, column=0, sticky="w", pady=5)
        ttk.Entry(form, textvariable=self.notes).grid(row=2, column=1, columnspan=3, sticky="ew", pady=5)
        self.contact_detail = ttk.Label(form, text="分值范围 0～100，留空跟随历史行为评分。", wraplength=780, style="Muted.TLabel")
        self.contact_detail.grid(row=3, column=0, columnspan=4, sticky="w", pady=5)
        ttk.Button(form, text="新增 / 清空表单", command=self.clear_contact).grid(row=4, column=2, sticky="e", pady=(5, 0))
        self.contact_save_button = ttk.Button(form, text="保存联系人", command=self.save_contact, style="Accent.TButton")
        self.contact_save_button.grid(row=4, column=3, sticky="e", pady=(5, 0))

    def _fill_profiles(self, profiles):
        self.contacts_tree.delete(*self.contacts_tree.get_children())
        self.profiles = {}
        for index, profile in enumerate(profiles):
            iid = f"contact_{index}"
            self.profiles[iid] = profile
            self.contacts_tree.insert("", "end", iid=iid, values=(profile.get("phone_number", ""),
                profile.get("display_name", ""), LIST_LABELS.get(profile.get("list_status"), "普通号码"),
                profile.get("trust_score", 50), profile.get("history_trust_score", 50), profile.get("call_count", 0),
                profile.get("last_summary", "")))

    def refresh_contacts(self):
        try:
            self._fill_profiles(self.controller.caller_store.list_profiles())
        except (OSError, ValueError, TypeError) as error:
            self.show_error("联系人读取失败", error)

    def clear_contact(self):
        for variable in (self.number, self.display_name, self.override, self.notes):
            variable.set("")
        self.list_status.set(LIST_LABELS["normal"])
        self.contacts_tree.selection_remove(*self.contacts_tree.selection())
        self.contact_detail.configure(text="人工分留空时使用历史行为评分；此操作只清空表单，不删除记录。")

    def select_contact(self, _event=None):
        selection = self.contacts_tree.selection()
        if not selection:
            return
        profile = self.profiles[selection[0]]
        self.number.set(profile.get("phone_number", ""))
        self.display_name.set(profile.get("display_name", ""))
        self.list_status.set(LIST_LABELS.get(profile.get("list_status"), LIST_LABELS["normal"]))
        self.override.set("" if profile.get("trust_override") is None else str(profile["trust_override"]))
        self.notes.set(profile.get("notes", ""))
        description = str(profile.get("trust_description", "历史行为分不代表身份认证。"))
        evidence = profile.get("evidence") or []
        if evidence:
            description += "  最近依据：" + json.dumps(evidence[-2:] if isinstance(evidence, list) else evidence, ensure_ascii=False)[:240]
        self.contact_detail.configure(text=description)

    def save_contact(self):
        try:
            status = next(key for key, label in LIST_LABELS.items() if label == self.list_status.get())
            self.controller.save_contact(self.number.get().strip(), self.display_name.get().strip(), status,
                self.override.get(), self.notes.get().strip())
            self.refresh_contacts()
            self.hint.configure(text="联系人已保存，下次来电生效。人工评分及名单不构成身份认证。")
        except (OSError, ValueError, TypeError, RuntimeError, StopIteration) as error:
            self.show_error("联系人保存失败", error)

    def _build_history(self):
        page = self.history_page
        page.columnconfigure(0, weight=1)
        page.rowconfigure(1, weight=1)
        page.rowconfigure(2, weight=1)
        bar = ttk.Frame(page)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(bar, text="最近 100 通来电；点击查看号码、状态、回复与检索证据。", style="Muted.TLabel").pack(side="left")
        ttk.Button(bar, text="刷新", command=self.refresh_history).pack(side="right")
        frame, self.history_tree = self._tree(page, (("time", "时间", 150), ("number", "号码", 130),
            ("type", "类型", 100), ("action", "建议动作", 115), ("summary", "回复 / 摘要", 360)))
        frame.grid(row=1, column=0, sticky="nsew")
        self.history_tree.bind("<<TreeviewSelect>>", self.select_history)
        self.history_detail = scrolledtext.ScrolledText(page, height=7, wrap="word", relief="flat", state="disabled")
        self.history_detail.grid(row=2, column=0, sticky="nsew", pady=(10, 0))

    def _fill_history(self, records):
        self.history_tree.delete(*self.history_tree.get_children())
        self.records = {}
        for index, record in enumerate(records):
            iid = f"history_{index}"
            self.records[iid] = record
            stamp = record.get("created_at", record.get("timestamp", ""))
            if isinstance(stamp, (int, float)):
                try:
                    stamp = datetime.fromtimestamp(stamp).strftime("%m-%d %H:%M:%S")
                except (ValueError, OSError, OverflowError):
                    stamp = str(stamp)
            card = record.get("notification_card") or {}
            self.history_tree.insert("", "end", iid=iid, values=(stamp, record.get("caller_number", ""),
                record.get("call_type_name", record.get("type_id", "")), record.get("final_action", ""),
                record.get("agent_reply") or card.get("body", "")))

    def refresh_history(self):
        try:
            self._fill_history(self.controller.history_store.recent(limit=100))
        except (OSError, ValueError, TypeError) as error:
            self.show_error("来电记录读取失败", error)

    def select_history(self, _event=None):
        selection = self.history_tree.selection()
        if selection:
            self.history_detail.configure(state="normal")
            self.history_detail.delete("1.0", "end")
            self.history_detail.insert("1.0", json.dumps(self.records[selection[0]], ensure_ascii=False, indent=2))
            self.history_detail.configure(state="disabled")

    def save_owner(self):
        try:
            try:
                minutes = int(self.duration.get().strip())
            except (TypeError, ValueError):
                raise ValueError("状态持续分钟数必须是整数；0 表示直到手动修改。") from None
            values = {"presence_mode": next(key for key, label in PRESENCE_LABELS.items() if label == self.mode.get()),
                "reason": self.reason.get().strip(), "duration_minutes": minutes,
                "allowed_dropoff": self.dropoff.get().strip(), "allow_order_changes": self.order_changes.get(),
                "callback_note": self.callback.get().strip()}
            self.owner = self.controller.save_owner(values)
            self.state_summary.configure(text=owner_summary(self.owner).replace("\n", "  ·  "))
            self.hint.configure(text="机主状态与授权已保存，下条日常来电生效；评测案例不继承。")
        except (OSError, ValueError, TypeError, RuntimeError, StopIteration) as error:
            self.show_error("机主设置保存失败", error)

    def show_error(self, title, error):
        messagebox.showerror(title, safe_error(error), parent=self)
