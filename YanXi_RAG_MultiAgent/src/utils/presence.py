"""
机主状态管理模块
----------------
让机主告诉 AI 当前在做什么，AI 据此调整来电处理策略。

状态模式:
  free (空闲)    — 正常处理，重要来电转接
  busy (忙碌)    — 提高转接门槛，非紧急留消息
  dnd  (免打扰)  — 只接极端紧急来电，其余一律留消息
  driving (开车) — 全代接，告知"机主在开车"

使用方式（文本模式下）:
  /free                   查看当前状态
  /busy 开会 30分钟       设忙碌+原因+时长
  /dnd 睡觉了             设免打扰
  /driving                设开车模式
  /free                   恢复空闲

代码中使用:
  from src.utils.presence import UserPresence
  presence = UserPresence()
  presence.set("busy", reason="开会中", duration_min=30)
  print(presence.get_reply_prefix())  # → "机主正在开会中，"
"""

import time
from dataclasses import dataclass, field

from src.utils.logger import setup_logger

logger = setup_logger(__name__)


@dataclass
class PresenceState:
    """机主当前状态"""
    mode: str = "free"            # free / busy / dnd / driving
    reason: str = ""              # 具体在做什么，如"开会中"、"学习中"
    since: float = 0.0            # 状态设置时间戳
    duration_min: int = 0         # 预计持续分钟数，0=手动解除
    auto_reply: str = ""          # 自定义自动回复（可选）


# 各模式的默认行为配置
MODE_CONFIG = {
    "free": {
        "label": "空闲",
        "forward_threshold": "normal",    # 正常门槛：亲友+有事→转接
        # 空闲模式不说"不方便接电话"，而是先问清来意再决定
        "default_reply": "",
        "allow_business_dialog": True,
    },
    "busy": {
        "label": "忙碌",
        "forward_threshold": "high",
        "default_reply": "机主正在忙，",
        "allow_business_dialog": True,
    },
    "dnd": {
        "label": "免打扰",
        "forward_threshold": "extreme",
        "default_reply": "机主已开启免打扰，",
        "allow_business_dialog": False,
    },
    "driving": {
        "label": "开车中",
        "forward_threshold": "high",
        "default_reply": "机主正在开车，",
        "allow_business_dialog": False,
    },
}


class UserPresence:
    """
    机主状态管理器（单例）。

    机主通过设置状态告诉 AI 自己在做什么，
    AI 根据状态调整来电处理策略和回复话术。

    使用示例:
        presence = UserPresence()
        presence.set("busy", "开会中", duration_min=30)
        prefix = presence.get_reply_prefix()  # "机主正在开会中，"
        if presence.should_forward("high"):
            ...
    """

    _instance = None

    def __new__(cls):
        instance = super().__new__(cls)
        instance._state = PresenceState()
        return instance

    def set(self, mode: str, reason: str = "", duration_min: int = 0, auto_reply: str = ""):
        """
        更新机主状态。

        参数:
            mode: 状态模式 (free/busy/dnd/driving)
            reason: 具体原因，如"开会中"、"学习"、"睡觉"
            duration_min: 预计持续分钟，0 表示手动解除
            auto_reply: 自定义自动回复，为空则用默认话术
        """
        if mode not in MODE_CONFIG:
            logger.warning(f"未知状态模式: {mode}，使用 'busy' 代替")
            mode = "busy"

        self._state.mode = mode
        self._state.reason = reason
        self._state.since = time.time()
        self._state.duration_min = duration_min
        self._state.auto_reply = auto_reply

        config = MODE_CONFIG[mode]
        label = config["label"]
        duration_str = f"({duration_min}分钟)" if duration_min > 0 else ""
        reason_str = f": {reason}" if reason else ""
        logger.info(f"机主状态已更新 → {label}{reason_str} {duration_str}")

    def get_mode(self) -> str:
        """获取当前状态模式。"""
        # 检查定时过期
        if self._state.duration_min > 0:
            elapsed = (time.time() - self._state.since) / 60
            if elapsed >= self._state.duration_min:
                logger.info(f"状态已自动过期 ({self._state.mode} → free)")
                self._state.mode = "free"
                self._state.reason = ""
                self._state.duration_min = 0
                self._state.auto_reply = ""
        return self._state.mode

    def get_reason(self) -> str:
        """获取当前状态原因。"""
        self.get_mode()  # 触发过期检查
        return self._state.reason

    def get_config(self) -> dict:
        """获取当前模式的完整配置。"""
        mode = self.get_mode()
        return MODE_CONFIG.get(mode, MODE_CONFIG["free"])

    def get_reply_prefix(self) -> str:
        """
        获取 Agent 回复前缀，告知来电者机主当前状态。

        返回:
            str: 如 "机主正在开会，" 或 "机主暂时不方便接电话，我帮您转达，"
        """
        self.get_mode()  # 触发过期检查
        if self._state.auto_reply:
            return self._state.auto_reply

        mode = self._state.mode
        reason = self._state.reason
        config = MODE_CONFIG.get(mode, MODE_CONFIG["free"])
        default = config["default_reply"]

        # 非空闲模式：有原因就加上原因
        if mode != "free" and reason:
            label = config["label"]
            return f"机主正在{label}（{reason}），"

        return default

    def should_forward(self, urgency: str, relationship: str = "陌生人") -> bool:
        """
        判断当前状态下是否应该转接来电给机主。

        参数:
            urgency: 紧急程度 (high/medium/low)
            relationship: 来电者与机主的关系

        返回:
            bool: 是否应该转接
        """
        if self.get_mode() == "driving":
            return False
        config = self.get_config()
        threshold = config["forward_threshold"]

        # extreme: 几乎不转接，除了直系亲属+high
        if threshold == "extreme":
            if urgency == "high" and relationship in ("直系亲属", "父母", "子女", "配偶"):
                return True
            return False

        # high: 亲属+high/medium 转接
        if threshold == "high":
            if urgency == "high":
                return True
            if urgency == "medium" and relationship not in ("陌生人",):
                return True
            return False

        # normal: 正常逻辑
        return urgency in ("high", "medium") and relationship != "陌生人"

    def allow_business_dialog(self) -> bool:
        """
        当前状态下是否允许与外卖员/快递员多轮对话，
        False 则直接让放门口/驿站。
        """
        return self.get_config()["allow_business_dialog"]

    def get_summary(self) -> str:
        """获取当前状态的文本摘要。"""
        mode = self.get_mode()
        config = MODE_CONFIG.get(mode, MODE_CONFIG["free"])
        reason = self.get_reason()

        if mode == "free":
            return "空闲中"

        text = config["label"]
        if reason:
            text += f"（{reason}）"
        if self._state.duration_min > 0:
            remaining = max(0, self._state.duration_min - (time.time() - self._state.since) / 60)
            text += f" 剩余约{remaining:.0f}分钟"
        return text

    def reset(self):
        """重置为空闲状态。"""
        self.set("free")


# 全局单例
_presence = None


def get_presence() -> UserPresence:
    """获取 UserPresence 全局单例。"""
    global _presence
    if _presence is None:
        _presence = UserPresence()
    return _presence
