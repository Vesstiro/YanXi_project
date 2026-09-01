"""
机主习惯学习模块 (HabitLearner)
================================
通过机主与言犀助手的对话，学习记录机主的日常习惯和日程安排，
自动调整来电处理策略。

核心功能:
  1. 习惯记录: 机主告诉助手自己的日程/习惯
  2. 状态推断: 根据当前时间和已记录的习惯，自动推断机主状态
  3. 规则生成: 根据习惯自动生成来电处理规则

示例对话:
  机主: "我要自习一下午"
  助手: "好的，我记下了。今天下午您在自习，我会把非紧急来电都代接处理，
        只有紧急来电才会响铃打扰您。自习结束后记得告诉我哦～"

  机主: "我每天晚上11点到早上7点睡觉"
  助手: "已记录您的作息习惯：每晚23:00-07:00为睡眠时间。
        这段时间内，只有紧急来电会响铃，其他一律代接留消息。"

数据结构:
  Habit:
    - habit_id:     唯一标识
    - habit_type:   习惯类型 (schedule/recurring/preference)
    - description:  习惯描述
    - time_range:   时间范围 (start_hour, start_min, end_hour, end_min)
    - days_of_week: 适用的星期 (0=周一, 6=周日)
    - presence_mode: 对应的机主状态
    - priority:     优先级
    - created_at:   创建时间
    - expires_at:   过期时间 (一次性日程)

使用方式:
    from src.habit.habit_learner import HabitLearner

    learner = HabitLearner(config)
    # 机主对话学习
    result = learner.learn_from_conversation("我要自习一下午")
    # 查询当前状态
    mode = learner.infer_presence_mode()
    # 获取所有习惯
    habits = learner.get_all_habits()
"""

import json
import re
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Optional

from src.core.llm_client import LLMClient
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


class HabitType(Enum):
    """习惯类型"""
    SCHEDULE = "schedule"       # 一次性日程 (如"今天下午自习")
    RECURRING = "recurring"     # 周期性习惯 (如"每天晚上11点睡觉")
    PREFERENCE = "preference"   # 偏好设置 (如"外卖都放门口")


@dataclass
class Habit:
    """习惯数据结构"""
    habit_id: str = ""
    habit_type: str = ""
    description: str = ""
    # 时间范围
    start_hour: int = -1        # -1 表示不限定
    start_minute: int = 0
    end_hour: int = -1
    end_minute: int = 0
    # 适用星期 (0=周一, 6=周日, 空列表=每天)
    days_of_week: list[int] = field(default_factory=list)
    # 对应的机主状态
    presence_mode: str = "free"
    # 优先级 (1-10, 越大越优先)
    priority: int = 5
    # 时间戳
    created_at: float = 0.0
    expires_at: float = 0.0     # 0=永不过期
    # 原始对话
    source_text: str = ""

    def __post_init__(self):
        if not self.habit_id:
            self.habit_id = f"habit_{uuid.uuid4().hex[:8]}"
        if not self.created_at:
            self.created_at = time.time()

    def is_active(self, now: Optional[float] = None) -> bool:
        """判断习惯当前是否生效。"""
        now = now or time.time()

        # 检查过期
        if self.expires_at > 0 and now > self.expires_at:
            return False

        # 检查时间范围
        if self.start_hour >= 0:
            import datetime
            dt = datetime.datetime.fromtimestamp(now)
            current_minutes = dt.hour * 60 + dt.minute
            start_minutes = self.start_hour * 60 + self.start_minute
            end_minutes = self.end_hour * 60 + self.end_minute

            # 处理跨午夜的情况 (如 23:00 - 07:00)
            if end_minutes < start_minutes:
                if not (current_minutes >= start_minutes or current_minutes <= end_minutes):
                    return False
            else:
                if not (start_minutes <= current_minutes <= end_minutes):
                    return False

            # 检查星期
            if self.days_of_week:
                weekday = dt.weekday()  # 0=周一
                if weekday not in self.days_of_week:
                    return False

        return True

    def to_dict(self) -> dict:
        return asdict(self)


# ============================================================
# Prompt 模板
# ============================================================

HABIT_LEARNING_SYSTEM_PROMPT = """你是言犀AI助手，负责学习机主的日常习惯和日程安排。

## 你的任务
从机主的话语中提取结构化的习惯信息，用于自动调整来电处理策略。

## 提取规则
1. 识别机主提到的活动和时间
2. 判断这是"一次性日程"还是"周期性习惯"
3. 推断该活动对应的机主状态：
   - free: 空闲，正常处理来电
   - busy: 忙碌（学习、工作、开会等），非紧急不响铃
   - dnd: 免打扰（睡觉、午休等），几乎不响铃
   - driving: 开车，全代接
4. 估算活动持续时间

## 常见模式识别
- "我要自习一下午" → 一次性日程，14:00-18:00，busy
- "我每天晚上11点到早上7点睡觉" → 周期性习惯，23:00-07:00，dnd
- "我周一到周五上班" → 周期性习惯，09:00-18:00，busy
- "我在开车" → 一次性日程，当前时间+2小时，driving
- "外卖都放门口" → 偏好设置，不影响状态
- "我下午有个面试" → 一次性日程，14:00-16:00，busy

## 输出格式 (JSON)
{
    "habit_type": "schedule" / "recurring" / "preference",
    "description": "习惯的简洁描述",
    "start_hour": 14,
    "start_minute": 0,
    "end_hour": 18,
    "end_minute": 0,
    "days_of_week": [],
    "presence_mode": "busy",
    "priority": 5,
    "duration_hours": 4,
    "is_recurring": false,
    "reply": "对机主的友好回复，确认已记录习惯，并说明来电处理策略的变化"
}

注意：
- 如果机主没有明确说时间，根据上下文合理推断
- days_of_week 为空数组表示每天适用
- 一次性日程的 duration_hours 用于计算过期时间
- reply 要自然友好，告知机主来电处理策略的变化
"""


class HabitLearner:
    """机主习惯学习器。"""

    def __init__(self, config: dict):
        self.config = config
        habit_cfg = config.get("habit", {})
        self.persist_path = Path(habit_cfg.get("persist_path", "./data/habits/habit_store.json"))
        self.persist_path.parent.mkdir(parents=True, exist_ok=True)

        self.llm = LLMClient(config)
        self._habits: list[Habit] = []
        self._load_habits()

    def _load_habits(self):
        """从文件加载已保存的习惯。"""
        if self.persist_path.exists():
            try:
                with open(self.persist_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._habits = [Habit(**h) for h in data.get("habits", [])]
                logger.info(f"已加载 {len(self._habits)} 条习惯记录")
            except Exception as e:
                logger.warning(f"加载习惯记录失败: {e}")
                self._habits = []

    def _save_habits(self):
        """保存习惯到文件。"""
        data = {
            "habits": [h.to_dict() for h in self._habits],
            "updated_at": time.time(),
        }
        with open(self.persist_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.debug(f"习惯记录已保存: {len(self._habits)} 条")

    def learn_from_conversation(self, user_text: str) -> dict:
        """
        从机主对话中学习习惯。

        参数:
            user_text: 机主说的话

        返回:
            dict: {
                "success": bool,
                "habit": Habit 或 None,
                "reply": str,  # 助手回复
                "mode_change": str 或 None,  # 状态变化描述
            }
        """
        messages = [
            {"role": "system", "content": HABIT_LEARNING_SYSTEM_PROMPT},
            {"role": "user", "content": f"机主说: \"{user_text}\"\n\n当前时间: {self._current_time_str()}"},
        ]

        result = self.llm.chat(
            messages=messages,
            response_type="json",
            default_response={},
        )

        if not result or not isinstance(result, dict):
            return {
                "success": False,
                "habit": None,
                "reply": "抱歉，我没有理解您的意思，能再说一遍吗？",
                "mode_change": None,
            }

        # 构建 Habit 对象
        habit = Habit(
            habit_type=result.get("habit_type", "schedule"),
            description=result.get("description", user_text),
            start_hour=result.get("start_hour", -1),
            start_minute=result.get("start_minute", 0),
            end_hour=result.get("end_hour", -1),
            end_minute=result.get("end_minute", 0),
            days_of_week=result.get("days_of_week", []),
            presence_mode=result.get("presence_mode", "free"),
            priority=result.get("priority", 5),
            source_text=user_text,
        )

        # 设置过期时间（一次性日程）
        if habit.habit_type == "schedule" and result.get("duration_hours", 0) > 0:
            habit.expires_at = time.time() + result["duration_hours"] * 3600

        # 保存习惯
        self._habits.append(habit)
        self._save_habits()

        # 清理过期习惯
        self._cleanup_expired()

        reply = result.get("reply", f"好的，已记录：{habit.description}")
        mode_change = None
        if habit.presence_mode != "free":
            mode_labels = {"busy": "忙碌", "dnd": "免打扰", "driving": "开车"}
            mode_change = f"机主状态已调整为「{mode_labels.get(habit.presence_mode, habit.presence_mode)}」"

        logger.info(f"学习到新习惯: {habit.description} → 状态={habit.presence_mode}")

        return {
            "success": True,
            "habit": habit,
            "reply": reply,
            "mode_change": mode_change,
        }

    def infer_presence_mode(self, now: Optional[float] = None) -> tuple[str, str]:
        """
        根据已记录的习惯推断当前机主状态。

        返回:
            tuple[str, str]: (状态模式, 原因描述)
        """
        now = now or time.time()
        active_habits = [h for h in self._habits if h.is_active(now)]

        if not active_habits:
            return "free", ""

        # 按优先级排序，取最高优先级的习惯
        active_habits.sort(key=lambda h: h.priority, reverse=True)
        top_habit = active_habits[0]

        return top_habit.presence_mode, top_habit.description

    def get_active_habits(self, now: Optional[float] = None) -> list[Habit]:
        """获取当前生效的习惯列表。"""
        now = now or time.time()
        return [h for h in self._habits if h.is_active(now)]

    def get_all_habits(self) -> list[Habit]:
        """获取所有习惯（含过期）。"""
        return list(self._habits)

    def remove_habit(self, habit_id: str) -> bool:
        """删除指定习惯。"""
        before = len(self._habits)
        self._habits = [h for h in self._habits if h.habit_id != habit_id]
        if len(self._habits) < before:
            self._save_habits()
            return True
        return False

    def end_current_activity(self, keyword: str = "") -> Optional[str]:
        """
        机主结束当前活动，清除相关的一次性日程。

        返回:
            str: 被清除的活动描述，或 None
        """
        now = time.time()
        active_schedules = [
            h for h in self._habits
            if h.habit_type == "schedule" and h.is_active(now)
        ]

        if keyword:
            active_schedules = [
                h for h in active_schedules
                if keyword in h.description
            ]

        if active_schedules:
            self._habits = [
                h for h in self._habits
                if h not in active_schedules
            ]
            self._save_habits()
            descriptions = ", ".join(h.description for h in active_schedules)
            logger.info(f"已结束活动: {descriptions}")
            return descriptions
        return None

    def _cleanup_expired(self):
        """清理过期的一次性日程。"""
        now = time.time()
        before = len(self._habits)
        self._habits = [h for h in self._habits if h.expires_at == 0 or h.expires_at > now]
        removed = before - len(self._habits)
        if removed > 0:
            self._save_habits()
            logger.debug(f"已清理 {removed} 条过期习惯")

    def _current_time_str(self) -> str:
        """获取当前时间的中文描述。"""
        import datetime
        now = datetime.datetime.now()
        weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        return (
            f"{now.year}年{now.month}月{now.day}日 "
            f"{weekdays[now.weekday()]} "
            f"{now.hour:02d}:{now.minute:02d}"
        )

    def get_habits_summary(self) -> str:
        """获取习惯摘要文本。"""
        if not self._habits:
            return "暂无习惯记录"

        lines = ["📋 已记录的习惯:"]
        for i, h in enumerate(self._habits, 1):
            status = "✅ 生效中" if h.is_active() else "⏸ 未生效"
            time_info = ""
            if h.start_hour >= 0:
                time_info = f" {h.start_hour:02d}:{h.start_minute:02d}-{h.end_hour:02d}:{h.end_minute:02d}"
            lines.append(f"  {i}. [{status}] {h.description}{time_info} → {h.presence_mode}")

        return "\n".join(lines)
