"""枚举常量定义（增强版）
来自 QM-newer 的基础枚举 + CC 的 16 种来电回复模板映射。
所有分类逻辑使用枚举常量，避免字符串硬编码。
"""

from enum import Enum


class CallType(Enum):
    """来电类型枚举（与 CC 版 call_types.py 的 type_id 对应）"""
    SCAM = ("scam", "诈骗拦截", "🦶")
    SCAM_RISK = ("scam_risk", "疑似诈骗", "⚠️")
    TELEMARKETING = ("telemarketing", "推销广告", "📞")
    GAME_PROMO = ("game_promo", "游戏推广", "🎮")
    FOOD_DELIVERY = ("food_delivery", "外卖配送", "🍝")
    EXPRESS = ("express", "快递送达", "📦")
    TAXI_ARRIVED = ("taxi_arrived", "网约车到达", "🚗")
    BANK = ("bank", "银行通知", "🏦")
    FAMILY = ("family", "家人来电", "👨‍👩‍👧")
    LEADER = ("leader", "领导来电", "👔")
    FRIEND = ("friend", "朋友来电", "👋")
    COLLEAGUE = ("colleague", "同事来电", "👋")
    CLIENT = ("client", "客户来电", "👋")
    INTERVIEW = ("interview", "面试通知", "📩")
    URGENT = ("urgent", "紧急来电", "❗")
    GENERAL = ("general", "普通来电", "📞")
    MEANINGLESS = ("meaningless", "无意义", "❓")

    def __init__(self, type_id: str, display_name: str, emoji: str):
        self.type_id = type_id
        self.display_name = display_name
        self.emoji = emoji

    @classmethod
    def from_id(cls, type_id: str) -> "CallType":
        for ct in cls:
            if ct.type_id == type_id:
                return ct
        return cls.GENERAL


class CallAction(Enum):
    """来电处理动作枚举"""
    REJECT = "reject"                    # 拒接
    FORWARD = "forward"                  # 转接机主
    PROXY = "proxy"                      # 代接（AI 对话）
    RECORD = "record"                    # 记录留言
    ASK = "ask"                          # 询问信息
    GENERAL_REPLY = "general_reply"      # 通用回复
    SUMMARY_CARD = "summary_card"        # 生成摘要卡片
    CONTINUE_CONVERSATION = "continue_conversation"  # 继续对话
    ERROR = "error"                      # 错误


class PresenceMode(Enum):
    """机主状态枚举"""
    FREE = ("free", "空闲")
    BUSY = ("busy", "忙碌")
    DND = ("dnd", "免打扰")
    DRIVING = ("driving", "开车中")

    def __init__(self, mode: str, label: str):
        self.mode = mode
        self.label = label


class ClassifyMethod(Enum):
    """分类方法枚举"""
    KEYWORD = "keyword"
    RAG = "rag"
    LLM = "llm"
    DEFAULT = "default"
    MEANINGLESS_DETECT = "meaningless_detect"
    NONE = "none"


class LLMBackend(Enum):
    """LLM 后端枚举"""
    DEEPSEEK = "deepseek"
    ZHIPU = "zhipu"
    QWEN = "qwen"


def get_api_action(call_type_id: str, presence_mode: str) -> str:
    """根据来电类型和机主状态获取处理动作（供 nodes.py 和 api.py 使用）"""
    from src.agents.call_types import get_call_type, get_action
    call_type = get_call_type(call_type_id)
    return get_action(call_type, presence_mode)
