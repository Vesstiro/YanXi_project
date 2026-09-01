"""Thread-safe local context stores, independent of GUI, models and API keys.

Caller scores describe *observed conversation behaviour*, not authenticated
identity. Only the owner can put a number on a whitelist or blacklist. A model
failure or an unknown category never becomes evidence of fraud.

Writes replace complete JSON files atomically. Malformed existing files are
preserved and reported rather than silently replaced with an empty database.
Locks are shared by all instances for the same path within this process;
running two separate application processes against one data folder is not
supported.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import unicodedata
import uuid


ROOT = Path(__file__).resolve().parents[2]
_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, threading.RLock] = {}
_MODES = {"free", "busy", "dnd", "driving"}
_LIST_STATUSES = {"normal", "whitelist", "blacklist"}


class StoreDataError(ValueError):
    """Existing local data could not be read safely; its contents are intact."""


def _lock_for(path: Path):
    key = os.path.normcase(str(path.resolve()))
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def _invalid_constant(value: str):
    raise ValueError(f"JSON 不允许非有限数值：{value}")


def _read_json(path: Path):
    try:
        with path.open("r", encoding="utf-8-sig") as stream:
            return json.load(stream, parse_constant=_invalid_constant)
    except (OSError, UnicodeError, ValueError) as exc:
        raise StoreDataError(
            f"无法读取本地数据文件：{path}。原文件未被覆盖，请备份后检查 JSON 格式或文件权限。"
        ) from exc


def _atomic_json(path: Path, value) -> None:
    # Validate before creating directories or temporary files.
    content = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", prefix=f".{path.name}-",
            suffix=".tmp", dir=path.parent, delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _string(value, name: str, maximum: int = 2000) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name}必须是文字。")
    result = value.strip()
    if len(result) > maximum:
        raise ValueError(f"{name}不能超过 {maximum} 个字符。")
    return result


def _finite_number(value, name: str, minimum: float = 0.0, maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name}必须是有效数值。")
    value = float(value)
    if not math.isfinite(value) or value < minimum or (maximum is not None and value > maximum):
        bound = f"{minimum:g}～{maximum:g}" if maximum is not None else f"不小于 {minimum:g}"
        raise ValueError(f"{name}必须{('在 ' + bound + ' 之间') if maximum is not None else bound}。")
    return value


def normalize_number(number: str | None) -> str:
    """Normalize conservative phone formatting without guessing missing digits.

    Chinese mobile numbers with +86/0086/86 share one profile. International
    numbers retain their explicit country code. Mainland landlines (including
    a +86 prefix) keep their original national/international form: guessing an
    omitted trunk zero could incorrectly merge different callers. Visible
    extension markers and malformed numbers are rejected, not stripped.
    Only an empty/None value represents an anonymous caller.
    """
    if number is None:
        return ""
    if not isinstance(number, str):
        raise ValueError("电话号码必须是文字，以免丢失开头的 0 或国家区号。")
    raw = unicodedata.normalize("NFKC", number).strip()
    if not raw:
        return ""
    if not re.fullmatch(r"[0-9+()\s-]+", raw):
        raise ValueError("电话号码含不支持的字符或分机号；请填写完整号码，未知号码请留空。")
    depth = 0
    for char in raw:
        if char == "(":
            depth += 1
            if depth > 1:
                raise ValueError("电话号码的括号格式不正确。")
        elif char == ")":
            depth -= 1
            if depth < 0:
                raise ValueError("电话号码的括号格式不正确。")
    if depth:
        raise ValueError("电话号码的括号格式不正确。")
    grouped = re.sub(r"[()\s]", "", raw)
    if re.fullmatch(r"0[0-9]{2,3}-[0-9]{7,8}-[0-9]{1,6}", grouped):
        raise ValueError("请勿将分机号拼接到座机主号码；分机可写入联系人备注。")
    compact = re.sub(r"[()\s-]", "", raw)
    if compact.startswith("00"):
        compact = "+" + compact[2:]
    if re.fullmatch(r"(?:\+86|86)1[3-9][0-9]{9}", compact):
        return compact[-11:]
    if compact.startswith("+"):
        if not re.fullmatch(r"\+[1-9][0-9]{6,14}", compact):
            raise ValueError("国际号码应为 +国家区号及号码（共 7～15 位数字），不包含分机。")
        # Do not turn a visibly appended mainland extension into a new number.
        if compact.startswith("+86"):
            national = compact[3:]
            if not re.fullmatch(r"(?:1[3-9][0-9]{9}|[1-9][0-9]{8,10}|0[0-9]{9,11})", national):
                raise ValueError("中国号码长度不正确，请将分机号单独记入联系人备注。")
        return compact
    if not re.fullmatch(r"[0-9]+", compact):
        raise ValueError("电话号码中的 + 号只能放在国家区号前。")
    if compact.startswith("0"):
        # 010 and 02x area codes have 7/8-digit subscribers; other area codes
        # have three digits after the trunk zero and 7/8-digit subscribers.
        area_digits = 3 if compact.startswith(("010", "02")) else 4
        if len(compact) not in {area_digits + 7, area_digits + 8}:
            raise ValueError("座机号码长度不正确，请勿将分机号拼接到主号码。")
    elif len(compact) not in {3, 5, 6, 7, 8, 10, 11}:
        raise ValueError("电话号码长度或格式不正确；国际号码请填写 +国家区号。")
    return compact


class OwnerSettingsStore:
    """Owner presence and explicit permissions, persisted under a project root."""

    def __init__(self, root=ROOT):
        self.root = Path(root)
        self.path = self.root / "data" / "owner" / "settings.json"
        self._lock = _lock_for(self.path)

    @staticmethod
    def _defaults() -> dict:
        return {
            "presence_mode": "free", "reason": "", "duration_minutes": 0,
            "updated_at": time.time(), "expires_at": 0.0, "allowed_dropoff": "",
            "allow_order_changes": False, "callback_note": "",
        }

    @classmethod
    def _validate(cls, raw: Mapping) -> dict:
        if not isinstance(raw, Mapping):
            raise ValueError("机主设置必须是一个 JSON 对象。")
        result = cls._defaults()
        result.update({key: raw[key] for key in result if key in raw})
        if result["presence_mode"] not in _MODES:
            raise ValueError("机主状态必须是 free、busy、dnd 或 driving。")
        for key, label in (("reason", "状态说明"), ("allowed_dropoff", "允许放置的位置"),
                           ("callback_note", "回电说明")):
            result[key] = _string(result[key], label)
        minutes = result["duration_minutes"]
        if isinstance(minutes, bool) or not isinstance(minutes, int) or not 0 <= minutes <= 10080:
            raise ValueError("状态时长必须是 0～10080 的整数分钟，0 表示手动解除。")
        if not isinstance(result["allow_order_changes"], bool):
            raise ValueError("允许修改订单必须是布尔值。")
        for key in ("updated_at", "expires_at"):
            result[key] = _finite_number(result[key], key)
        return result

    def _load_locked(self) -> dict:
        if not self.path.exists():
            return self._defaults()
        try:
            result = self._validate(_read_json(self.path))
        except (TypeError, ValueError) as exc:
            if isinstance(exc, StoreDataError):
                raise
            raise StoreDataError(f"机主设置格式不正确：{self.path}。原文件未被覆盖。") from exc
        now = time.time()
        if result["expires_at"] and now >= result["expires_at"]:
            result.update(presence_mode="free", reason="", duration_minutes=0,
                          expires_at=0.0, updated_at=now)
            _atomic_json(self.path, result)
        elif result["presence_mode"] == "free":
            # Never expose an obsolete busy reason from an older saved file.
            result.update(reason="", duration_minutes=0, expires_at=0.0)
        return result

    def load(self) -> dict:
        with self._lock:
            return deepcopy(self._load_locked())

    def save(self, settings: Mapping) -> dict:
        if not isinstance(settings, Mapping):
            raise ValueError("机主设置必须是一个字典。")
        with self._lock:
            merged = self._load_locked()
            merged.update(settings)
            result = self._validate(merged)
            now = time.time()
            result["updated_at"] = now
            if result["presence_mode"] == "free":
                result.update(reason="", duration_minutes=0, expires_at=0.0)
            else:
                result["expires_at"] = now + result["duration_minutes"] * 60 if result["duration_minutes"] else 0.0
            _atomic_json(self.path, result)
            return deepcopy(result)


def _new_profile(number: str) -> dict:
    return {
        "phone_number": number, "display_name": "", "notes": "", "list_status": "normal",
        "trust_override": None, "trust_score": 50.0, "history_trust_score": 50.0,
        "call_count": 0, "first_call_time": 0.0, "last_call_time": 0.0,
        "last_type_id": "", "last_summary": "", "last_risk_level": "unknown",
        "evidence": [], "processed_call_ids": [], "trust_description": "",
    }


def _effective_profile(profile: dict) -> dict:
    result = deepcopy(profile)
    status, override = result["list_status"], result["trust_override"]
    if status == "blacklist":
        result["trust_score"] = 0.0
        source = "机主手动设置黑名单"
    elif status == "whitelist":
        result["trust_score"] = 100.0
        source = "机主手动设置白名单"
    elif override is not None:
        result["trust_score"] = override
        source = "机主手动信任评分"
    else:
        result["trust_score"] = result["history_trust_score"]
        source = "历史来电行为参考评分" if result["call_count"] else "尚无历史记录，初始中性评分"
    if not result["phone_number"]:
        source = "未提供来电号码，不建立或合并号码画像"
    result["trust_description"] = f"{source}；分值仅供参考，不代表来电者身份已经核实。"
    return result


_TYPE_ALIASES = {
    "normal": "general", "express_delivery": "express", "delivery": "express",
    "fraud": "scam", "promotion": "telemarketing", "friends": "friend",
}
_BENIGN_TYPES = {
    "general", "express", "food_delivery", "taxi_arrived", "bank", "family",
    "friend", "leader", "colleague", "client", "interview", "urgent", "service",
}
_UNKNOWN_RISKS = {"", "unknown", "error", "api_error", "unavailable", "uncertain", "degraded", "未判断", "未知"}
_RISK_ALIASES = {"none": "low", "safe": "low", "benign": "low", "低": "low", "低风险": "low",
                 "中": "medium", "中风险": "medium", "高": "high", "高风险": "high", "critical": "high",
                 "1级-安全": "low", "2级-低风险": "unknown", "3级-中风险": "medium",
                 "4级-高风险": "high", "5级-高危": "high"}


def _behaviour_change(type_id: str, risk_level: str) -> tuple[float, str]:
    kind = _TYPE_ALIASES.get(type_id, type_id)
    risk = _RISK_ALIASES.get(risk_level, risk_level)
    if risk in _UNKNOWN_RISKS or risk not in {"low", "medium", "high"}:
        return 0.0, "风险未知或服务异常，仅记录来电，不据此判断诈骗或扣分。"
    known_types = _BENIGN_TYPES | {"scam", "scam_risk", "telemarketing", "game_promo", "meaningless"}
    if kind not in known_types:
        return 0.0, "未识别的分类，仅记录来电，不因分类标识不同而扣分。"
    if kind in {"scam", "scam_risk"}:
        if risk == "high":
            return (-25.0 if kind == "scam" else -15.0), "模型判断本次内容具有高风险迹象；这不是对号码身份的事实认定。"
        if risk == "medium":
            return -8.0, "模型判断本次内容有可疑迹象，需要人工核实。"
        return 0.0, "分类与低风险结论不一致，不据此确认诈骗或调整评分。"
    if risk == "high":
        return -10.0, "本次内容被模型判断为高风险，建议复核；不自动加入黑名单。"
    if risk == "medium":
        return 0.0, "本次内容需进一步核实，保留原历史评分。"
    if kind in {"telemarketing", "game_promo"}:
        return -2.0, "本次被判断为推销内容，仅作轻微信任参考调整，不等同诈骗。"
    if kind in _BENIGN_TYPES:
        return 1.0, "本次内容被判断为普通低风险来电，轻微增加行为参考分，不认证身份。"
    return 0.0, "证据不足，仅记录来电，不调整行为参考评分。"


class CallerStore:
    """Per-number profiles; reads never create unknown/anonymous contacts."""

    def __init__(self, path=None):
        self.path = Path(path) if path is not None else ROOT / "data" / "callers" / "profiles.json"
        self._lock = _lock_for(self.path)

    @staticmethod
    def _validate_profile(number: str, raw) -> dict:
        if not isinstance(raw, Mapping) or normalize_number(number) != number or not number:
            raise ValueError("号码画像必须以规范号码为键。")
        result = _new_profile(number)
        result.update({key: raw[key] for key in result if key in raw})
        if result["phone_number"] != number or result["list_status"] not in _LIST_STATUSES:
            raise ValueError("号码画像中的号码或名单状态无效。")
        for key in ("display_name", "notes", "last_type_id", "last_summary", "last_risk_level"):
            result[key] = _string(result[key], key, 20000 if key == "last_summary" else 2000)
        result["history_trust_score"] = _finite_number(result["history_trust_score"], "历史行为评分", 0, 100)
        if result["trust_override"] is not None:
            result["trust_override"] = _finite_number(result["trust_override"], "人工信任评分", 0, 100)
        if isinstance(result["call_count"], bool) or not isinstance(result["call_count"], int) or result["call_count"] < 0:
            raise ValueError("来电次数必须为非负整数。")
        for key in ("first_call_time", "last_call_time"):
            result[key] = _finite_number(result[key], key)
        if not isinstance(result["evidence"], list) or any(not isinstance(item, dict) for item in result["evidence"]):
            raise ValueError("来电证据必须为对象列表。")
        if (not isinstance(result["processed_call_ids"], list)
                or any(not isinstance(item, str) or not item for item in result["processed_call_ids"])):
            raise ValueError("来电去重标识必须为非空字符串列表。")
        result["evidence"] = result["evidence"][-20:]
        return _effective_profile(result)

    def _load_locked(self) -> dict:
        if not self.path.exists():
            return {}
        raw = _read_json(self.path)
        try:
            if not isinstance(raw, dict):
                raise ValueError("号码画像文件必须为对象。")
            return {number: self._validate_profile(number, profile) for number, profile in raw.items()}
        except (TypeError, ValueError) as exc:
            raise StoreDataError(f"号码画像数据格式不正确：{self.path}。原文件未被覆盖。") from exc

    def get(self, number: str | None) -> dict:
        normalized = normalize_number(number)
        if not normalized:
            return _effective_profile(_new_profile(""))
        with self._lock:
            return _effective_profile(self._load_locked().get(normalized, _new_profile(normalized)))

    def list_profiles(self) -> list[dict]:
        with self._lock:
            profiles = [_effective_profile(value) for value in self._load_locked().values()]
            return sorted(profiles, key=lambda value: (-value["last_call_time"], value["phone_number"]))

    def set_contact(self, number: str, display_name: str = "", trust_override=None,
                    list_status: str = "normal", notes: str = "") -> dict:
        normalized = normalize_number(number)
        if not normalized:
            raise ValueError("保存联系人时必须填写电话号码；匿名来电不建立共享画像。")
        name = _string(display_name, "联系人姓名", 200)
        notes = _string(notes, "联系人备注")
        if list_status not in _LIST_STATUSES:
            raise ValueError("名单状态必须是 normal、whitelist 或 blacklist，且只能选择一种。")
        if trust_override is not None:
            trust_override = _finite_number(trust_override, "人工信任评分", 0, 100)
        with self._lock:
            profiles = self._load_locked()
            profile = profiles.get(normalized, _new_profile(normalized))
            profile.update(display_name=name, notes=notes, trust_override=trust_override, list_status=list_status)
            profile = _effective_profile(profile)
            profiles[normalized] = profile
            _atomic_json(self.path, profiles)
            return deepcopy(profile)

    def record_call(self, number: str | None, type_id: str, risk_level: str,
                    summary: str = "", call_id: str = "") -> dict:
        normalized = normalize_number(number)
        if not normalized:
            return _effective_profile(_new_profile(""))
        kind = _string(type_id, "来电分类", 100).lower()
        risk = _string(risk_level, "风险等级", 100).lower()
        summary = _string(summary, "来电摘要", 20000)
        identifier = _string(call_id, "来电标识", 512) or uuid.uuid4().hex
        with self._lock:
            profiles = self._load_locked()
            profile = profiles.get(normalized, _new_profile(normalized))
            if identifier in profile["processed_call_ids"]:
                return _effective_profile(profile)
            now = time.time()
            delta, explanation = _behaviour_change(kind, risk)
            before = profile["history_trust_score"]
            after = round(max(0.0, min(100.0, before + delta)), 4)
            profile["call_count"] += 1
            if profile["first_call_time"] == 0:
                profile["first_call_time"] = now
            profile.update(last_call_time=now, last_type_id=kind, last_summary=summary,
                           last_risk_level=risk, history_trust_score=after)
            profile["processed_call_ids"].append(identifier)
            profile["evidence"].append({
                "call_id": identifier, "timestamp": now, "type_id": kind,
                "risk_level": risk, "summary": summary, "score_delta": round(after - before, 4),
                "history_trust_score": after, "description": explanation,
            })
            profile["evidence"] = profile["evidence"][-20:]
            profile = _effective_profile(profile)
            profiles[normalized] = profile
            _atomic_json(self.path, profiles)
            return deepcopy(profile)


class HistoryStore:
    """One immutable JSON result per call ID; no mutable, shared JSONL append."""

    def __init__(self, directory=None):
        self.directory = Path(directory) if directory is not None else ROOT / "data" / "calls"
        self._lock = _lock_for(self.directory)

    def append(self, result: Mapping) -> Path:
        if not isinstance(result, Mapping):
            raise ValueError("来电记录必须是一个字典。")
        record = deepcopy(dict(result))
        identifier = _string(record.get("call_id", ""), "来电标识", 512) or uuid.uuid4().hex
        record["call_id"] = identifier
        record.setdefault("created_at", time.time())
        name = hashlib.sha256(identifier.encode("utf-8")).hexdigest() + ".json"
        path = self.directory / name
        with self._lock:
            if path.exists():
                existing = _read_json(path)
                if not isinstance(existing, dict) or existing.get("call_id") != identifier:
                    raise StoreDataError(f"来电记录标识不匹配：{path}。原文件未被覆盖。")
                # A retry must not rewrite already persisted evidence.
                return path
            _atomic_json(path, record)
            return path

    @staticmethod
    def _timestamp(record: dict, path: Path) -> float:
        value = record.get("created_at")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            return float(value)
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
            except (ValueError, OSError, OverflowError):
                pass
        return path.stat().st_mtime

    def recent(self, limit: int = 100) -> list[dict]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError("来电记录数量必须为非负整数。")
        if limit == 0:
            return []
        with self._lock:
            if not self.directory.exists():
                return []
            records = []
            for path in self.directory.glob("*.json"):
                record = _read_json(path)
                if not isinstance(record, dict) or not isinstance(record.get("call_id"), str) or not record["call_id"]:
                    raise StoreDataError(f"来电记录格式不正确：{path}。原文件未被覆盖。")
                records.append((self._timestamp(record, path), path.name, record))
            records.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
            return [deepcopy(entry[2]) for entry in records[:limit]]
