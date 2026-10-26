"""Local-only question matching. No network, LLM, audio, or call-log imports.

DPAPI protects the reference data at rest for the current Windows account. It is
not MPC or a boundary against that account, a compromised host, or screen access.
Submitted answers are compared immediately and never retained by a session.
"""
from __future__ import annotations

import base64
import ctypes
import json
import os
import secrets
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from src.context.stores import CallerStore, _atomic_json, normalize_number


CATEGORIES = ("日常联系", "工作业务", "预约服务", "配送联系", "其他事项")
QUESTION_SPECS = (
    ("name", "常用称呼", "请问您要联系的人怎么称呼？", "basic"),
    ("organization", "单位 / 学校", "您要联系的人所在的单位或学校是哪里？", "basic"),
    ("department", "部门 / 业务领域", "这次联系涉及哪个部门或业务方向？", "basic"),
    ("matter", "当前联系事项", "请简要说明这次联系的具体事项。", "matter"),
    ("agency", "事项关联机构", "您代表或关联的机构叫什么？", "matter"),
    ("appointment", "已约定的时间", "如果此前约过时间，请说明约定的日期或时段。", "matter"),
    ("custom", "自定义核验事项", "请说明双方预先约定的项目代称。", "matter"),
)
UNKNOWN_ANSWERS = frozenset(("不知道", "不清楚", "不适用", "无法回答", "拒绝回答", "没听清"))


class VerificationError(ValueError):
    """Safe error messages must never include reference or submitted answers."""


def normalize_answer(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    # Only explicit aliases match: no substring, fuzzy, or cloud judgement.
    return "".join(c for c in value if not c.isspace() and not unicodedata.category(c).startswith("P"))


def default_profile() -> dict:
    return {"question_count": 3, "category": "工作业务", "valid_until": "",
            "custom_question": QUESTION_SPECS[-1][2],
            "fields": {key: {"enabled": False, "answers": []} for key, *_ in QUESTION_SPECS}}


def validate_profile(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise VerificationError("核验资料格式无效。")
    count = raw.get("question_count")
    if type(count) is not int or count not in (3, 5, 7):
        raise VerificationError("请选择 3、5 或 7 个核验问题。")
    if raw.get("category") not in CATEGORIES:
        raise VerificationError("请选择支持的事项类别。")
    until = raw.get("valid_until", "")
    if not isinstance(until, str):
        raise VerificationError("事项有效期格式应为 YYYY-MM-DD，或留空。")
    if until:
        try:
            if date.fromisoformat(until).isoformat() != until:
                raise ValueError()
        except ValueError:
            raise VerificationError("事项有效期格式应为 YYYY-MM-DD，或留空。") from None
    custom = raw.get("custom_question", QUESTION_SPECS[-1][2])
    if not isinstance(custom, str) or not 1 <= len(custom.strip()) <= 120:
        raise VerificationError("自定义问题应为 1～120 个字符。")
    if normalize_answer(custom) in {normalize_answer(spec[2]) for spec in QUESTION_SPECS[:-1]}:
        raise VerificationError("自定义问题不能与其他问题重复。")
    fields = raw.get("fields")
    if not isinstance(fields, dict):
        raise VerificationError("核验字段格式无效。")
    checked = default_profile()
    checked.update(question_count=count, category=raw["category"], valid_until=until,
                   custom_question=custom.strip())
    for key, label, *_ in QUESTION_SPECS:
        row = fields.get(key)
        if not isinstance(row, dict) or type(row.get("enabled")) is not bool:
            raise VerificationError("核验字段授权格式无效。")
        if not row["enabled"]:
            continue  # Unauthorizing a field removes its stored answers.
        answers = row.get("answers")
        if not isinstance(answers, list) or not 1 <= len(answers) <= 8:
            raise VerificationError(f"请为已授权的“{label}”填写 1～8 个答案或别称。")
        aliases = []
        for answer in answers:
            if not isinstance(answer, str) or not 1 <= len(answer.strip()) <= 160:
                raise VerificationError(f"“{label}”的每个答案应为 1～160 个字符。")
            normalized = normalize_answer(answer)
            if not normalized or normalized in UNKNOWN_ANSWERS:
                raise VerificationError(f"“{label}”不能使用空答案或“不知道”等选项。")
            if normalized not in {normalize_answer(a) for a in aliases}:
                aliases.append(answer.strip())
        checked["fields"][key] = {"enabled": True, "answers": aliases}
    if sum(row["enabled"] for row in checked["fields"].values()) < count:
        raise VerificationError(f"已授权且有答案的不同问题不足 {count} 道，请补充或降低题数。")
    return checked


class WindowsProtector:
    """Windows user-scope DPAPI, without any portable plaintext fallback."""

    @staticmethod
    def _crypt(payload: bytes, decrypt: bool) -> bytes:
        if os.name != "nt":
            raise VerificationError("本地资料加密目前仅支持 Windows DPAPI。")
        from ctypes import wintypes

        class Blob(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

        crypt = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        pointer = ctypes.POINTER(Blob)
        function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
        function.argtypes = [pointer, ctypes.c_void_p, pointer, ctypes.c_void_p,
                             ctypes.c_void_p, wintypes.DWORD, pointer]
        function.restype = wintypes.BOOL
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        buffer = ctypes.create_string_buffer(payload)
        source = Blob(len(payload), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
        output = Blob()
        try:
            if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
                raise VerificationError("资料加密或解密失败，请使用创建资料的 Windows 账户；原文件未被覆盖。")
            return ctypes.string_at(output.data, output.size)
        finally:
            if output.data:
                ctypes.memset(output.data, 0, output.size)
                kernel.LocalFree(ctypes.cast(output.data, ctypes.c_void_p))
            ctypes.memset(buffer, 0, len(buffer))

    def protect(self, payload: bytes) -> bytes:
        return self._crypt(payload, False)

    def unprotect(self, payload: bytes) -> bytes:
        return self._crypt(payload, True)


class VerificationVault:
    def __init__(self, root: str | Path, *, protector=None):
        self.path = Path(root) / "data" / "verification" / "vault.json"
        self.protector = protector if protector is not None else WindowsProtector()

    def load(self) -> dict | None:
        if not self.path.exists():
            return None
        try:
            if self.path.stat().st_size > 1_000_000:
                raise ValueError()
            envelope = json.loads(self.path.read_text(encoding="utf-8"))
            if envelope.get("version") != 1 or envelope.get("protection") != "windows-user-dpapi":
                raise ValueError()
            sealed = base64.b64decode(envelope["sealed"], validate=True)
            return validate_profile(json.loads(self.protector.unprotect(sealed).decode("utf-8")))
        except (ValueError, TypeError, KeyError, AttributeError, UnicodeError, OSError):
            raise VerificationError("无法读取核验资料，请检查文件或 Windows 账户；原文件未被覆盖。") from None

    def save(self, raw: dict) -> dict:
        checked = validate_profile(raw)
        # Never silently overwrite a corrupted or different-account vault.
        if self.path.exists():
            self.load()
        content = json.dumps(checked, ensure_ascii=False).encode("utf-8")
        envelope = {"version": 1, "protection": "windows-user-dpapi",
                    "sealed": base64.b64encode(self.protector.protect(content)).decode("ascii")}
        _atomic_json(self.path, envelope)
        return checked

    def delete(self):
        self.path.unlink(missing_ok=True)


@dataclass(frozen=True)
class Question:
    key: str
    prompt: str
    aliases: tuple[str, ...] = field(repr=False)


@dataclass(frozen=True)
class VerificationSummary:
    status: str
    question_count: int
    matched: int = 0
    mismatched: int = 0
    answered: int = 0

    @property
    def checked(self):
        return self.matched + self.mismatched

    @property
    def unknown(self):
        return self.question_count - self.checked

    @property
    def accuracy(self):
        return self.matched / self.checked if self.checked else None

    @property
    def coverage(self):
        return self.checked / self.question_count if self.question_count else 0.0

    @property
    def recommendation(self):
        if self.status == "known":
            return "该号码是机主手动登记的联系人或白名单，跳过陌生来电问答；号码熟悉不代表身份已经核实。"
        if self.status == "blacklist":
            return "该号码在机主手动黑名单中，跳过问答。本页仅供模拟，不改变已有名单策略。"
        if self.status == "unavailable":
            return "本类事项的有效题库不足，未进行核验。建议机主自行确认来意；资料不足不表示对方存在欺骗。"
        notes = []
        if self.status in ("cancelled", "timeout"):
            notes.append("核验超时或中途结束，结果不完整。")
        if self.mismatched:
            notes.append("部分回答与预留资料不符，建议接听后进一步核实身份和联系事项。")
        elif self.checked and self.coverage >= 0.8:
            notes.append("已核对的信息较一致，可作为接听参考；仍不能证明身份真实或通话合法。")
        else:
            notes.append("可核对的信息不足，暂无法评估；不知道答案或拒答不等于欺骗。")
        notes.append("准确率不是合法概率。涉及转账、验证码或敏感资料时，请通过已知渠道另行核实。")
        return "\n".join(notes)

    def owner_text(self):
        if self.status in ("known", "blacklist"):
            return self.recommendation
        accuracy = "暂无法计算" if self.accuracy is None else f"{self.accuracy:.0%}（{self.matched} / {self.checked} 个可核对项）"
        return (f"设置题数：{self.question_count}\n已提交：{self.answered} / {self.question_count}\n"
                f"匹配：{self.matched} 项    不匹配：{self.mismatched} 项    无法核对 / 未回答：{self.unknown} 项\n\n"
                f"可核对回答准确率：{accuracy}\n核对覆盖率：{self.coverage:.0%}\n\n接听建议\n{self.recommendation}")


class VerificationSession:
    """One frozen question set. Caller-facing API exposes only current prompt."""
    def __init__(self, profile: dict, category: str, *, today=None, clock=None, timeout=120):
        checked = validate_profile(profile)
        self.question_count = checked["question_count"]
        self._clock = clock or time.monotonic
        self._timeout = timeout
        self._deadline = self._clock() + timeout
        self._matched = self._mismatched = self._answered = 0
        self._questions = []
        self._status = "active"
        current = today or date.today()
        matter_valid = (category == checked["category"] and
                        (not checked["valid_until"] or date.fromisoformat(checked["valid_until"]) >= current))
        candidates = []
        matter = []
        for key, _label, prompt, scope in QUESTION_SPECS:
            row = checked["fields"][key]
            if row["enabled"] and (scope == "basic" or matter_valid):
                question = Question(key, checked["custom_question"] if key == "custom" else prompt,
                                    tuple(normalize_answer(a) for a in row["answers"]))
                candidates.append(question)
                if scope == "matter":
                    matter.append(question)
        random = secrets.SystemRandom()
        if len(candidates) < self.question_count:
            self._status = "unavailable"
        else:
            # Include a current-matter question when available, then randomize.
            first = random.choice(matter) if matter else random.choice(candidates)
            self._questions = [first] + random.sample([q for q in candidates if q != first], self.question_count - 1)
            random.shuffle(self._questions)

    def __repr__(self):
        return f"VerificationSession(status={self._status!r}, answered={self._answered}, count={self.question_count})"

    @property
    def done(self):
        self.expire()
        return self._status != "active"

    @property
    def progress(self):
        return self._answered, self.question_count

    @property
    def current_prompt(self):
        return "" if self.done else self._questions[self._answered].prompt

    def expire(self):
        if self._status == "active" and self._clock() >= self._deadline:
            self.finish("timeout")

    def submit(self, answer: str):
        if self.done:
            raise VerificationError("本次核验已结束，不能继续提交。")
        if not isinstance(answer, str) or len(answer) > 500:
            raise VerificationError("回答最多 500 个字符。")
        normalized = normalize_answer(answer)
        if normalized and normalized not in UNKNOWN_ANSWERS:
            if normalized in self._questions[self._answered].aliases:
                self._matched += 1
            else:
                self._mismatched += 1
        self._answered += 1
        if self._answered == self.question_count:
            self.finish("complete")
        else:
            self._deadline = self._clock() + self._timeout
        # No submitted answer or correctness value is returned to the caller.

    def finish(self, reason="cancelled"):
        if reason not in ("complete", "cancelled", "timeout"):
            raise VerificationError("无效的核验结束方式。")
        if self._status == "active":
            self._status = reason
            self._questions.clear()
        return self.summary()

    def summary(self):
        self.expire()
        if self._status == "active":
            raise VerificationError("核验完成后才能向机主显示汇总。")
        return VerificationSummary(self._status, self.question_count, self._matched,
                                   self._mismatched, self._answered)


class VerificationController:
    """Read contact labels only; never update profiles, trust, logs, or jobs."""
    def __init__(self, root, *, vault=None, callers=None):
        self.vault = vault if vault is not None else VerificationVault(root)
        self.callers = callers if callers is not None else CallerStore(Path(root) / "data/profiles/caller_profiles.json")

    def start(self, number, category):
        if category not in CATEGORIES:
            raise VerificationError("请选择来电事项类别。")
        number = normalize_number(number)
        profile = self.callers.get(number)
        if profile["list_status"] == "blacklist":
            return VerificationSummary("blacklist", 0)
        if profile["list_status"] == "whitelist" or profile["display_name"].strip():
            return VerificationSummary("known", 0)
        reference = self.vault.load()
        if reference is None:
            raise VerificationError("请先由机主填写并保存核验资料。")
        return VerificationSession(reference, category)
