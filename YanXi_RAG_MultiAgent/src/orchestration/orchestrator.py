"""Compatibility facade: new RAG/multi-agent core, original evaluator contract.

No original keyword classifier / LangGraph handlers are invoked. Public rules are
read-only; profile and history paths always come from the supplied configuration.
"""
import copy
import json
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

from src.core.config import PROJECT_ROOT
from src.core.enums import CallType
from src.core.llm_client import LLMClient
from src.context.stores import CallerStore, HistoryStore, normalize_number
from src.utils.presence import UserPresence, PresenceState
from src.assistant.scheduler import CallAgentScheduler
from src.rag.retriever import RagRetriever
from src.desktop.settings import safe_error

def local_path(value):
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path

class CallOrchestrator:
    def __init__(self, config, *, llm_client=None, rag=None, auxiliary_client=None):
        self.config = copy.deepcopy(config)
        self._lock = threading.RLock()
        self.llm_client = llm_client or LLMClient(config)
        # Independent compatibility handle, never silently learns from untrusted callers.
        # Original EvaluationService instruments/closes these two distinct clients.
        if auxiliary_client is None:
            from src.knowledge.habit_learner import LLMClient as AuxiliaryClient
            auxiliary_client = AuxiliaryClient(config)
        self.habit_learner = SimpleNamespace(llm=auxiliary_client)
        if auxiliary_client is self.llm_client:
            raise ValueError("评测兼容客户端不能与主客户端指向同一对象")
        self.rag = rag or RagRetriever(config, llm_client=self.llm_client)
        self._resources_closed = False
        self._model_close = self.llm_client.close
        # The unchanged evaluator closes llm_client instead of the facade. Hook
        # that lifecycle so the newly added RAG is released in both frontends.
        self.llm_client.close = self._close_main_resources
        self.scheduler = CallAgentScheduler(self.llm_client)
        self.profiles = CallerStore(local_path(config.get("caller_profile", {}).get("persist_path", "data/profiles/caller_profiles.json")))
        self.history = HistoryStore(local_path(config.get("call_log", {}).get("persist_dir", "data/call_logs")))
        self.notifications = HistoryStore(local_path(config.get("notification", {}).get("persist_path", "data/notifications")))
        self.presence = object.__new__(UserPresence)
        self.presence._state = PresenceState()
        self.owner = {}
        self.set_owner_context(config.get("owner", {}))

    def set_owner_context(self, owner):
        with self._lock:
            value = {"presence_mode": "free", "reason": "", "duration_minutes": 0,
                     "allowed_dropoff": "", "allow_order_changes": False, "callback_note": "", "expires_at": 0}
            value.update({key: owner[key] for key in value if key in owner})
            if value["presence_mode"] not in ("free", "busy", "dnd", "driving"):
                raise ValueError("机主状态无效")
            if type(value["allow_order_changes"]) is not bool:
                raise ValueError("订单授权必须为布尔值")
            if any(not isinstance(value[k], str) or len(value[k]) > 2000 for k in ("reason", "allowed_dropoff", "callback_note")):
                raise ValueError("机主说明必须为有限长度文字")
            self.owner = value
            self.presence.set(value["presence_mode"], value["reason"])

    def set_presence(self, mode, reason="", duration_min=0):
        if mode not in ("free", "busy", "dnd", "driving") or type(duration_min) is not int or not 0 <= duration_min <= 10080:
            raise ValueError("请选择有效机主状态和0至10080分钟时长")
        with self._lock:
            self.presence.set(mode, reason, duration_min)
            self.owner["expires_at"] = 0

    def _owner_context(self):
        if self.owner.get("expires_at", 0) and time.time() >= self.owner["expires_at"]:
            self.presence.set("free")
            self.owner.update(presence_mode="free", reason="", duration_minutes=0, expires_at=0)
        value = copy.deepcopy(self.owner)
        # Read current instance here: evaluator replaces presence after construction.
        value["presence_mode"] = self.presence.get_mode()
        value["reason"] = self.presence.get_reason()
        return value

    def run(self, call_text, caller_number=""):
        if not isinstance(call_text, str) or not call_text.strip() or len(call_text) > 16000:
            raise ValueError("来电文字不能为空或超过16000字符，请拆分输入")
        number = normalize_number(caller_number)
        with self._lock:
            started = time.monotonic()
            call_id = uuid.uuid4().hex
            owner = self._owner_context()
            profile = self.profiles.get(number)
            text = call_text.strip()
            if profile.get("list_status") == "blacklist":
                output = {"scene_analysis": {"summary": "此号码被机主手动设置为黑名单", "type_id": "general", "urgency": "low", "confidence": 1.0},
                    "risk_result": {}, "business_result": {}, "transfer_result": {}, "final_action": "reject",
                    "agent_reply": "您好，机主已设置不接受这个号码的来电。", "route_policy": {"reason": "手动黑名单优先"},
                    "policy_notes": ["按手动名单处理，未调用模型；这不是对来电诈骗性质的认定"],
                    "agent_trace": [{"step": "本地名单策略", "content": "手动黑名单拒接，不执行RAG与模型请求"}]}
                rag_hint, evidence, knowledge_hash, method = "", [], "", "manual_blacklist"
            else:
                rag_hint = self.rag.search(text)
                output = self.scheduler.handle(text, rag_hint, owner, profile)
                evidence = copy.deepcopy(self.rag.last_evidence)
                knowledge_hash, method = self.rag.knowledge_sha256, "rag_multi_agent"
                output["agent_trace"].insert(0, {"step": "混合RAG检索", "content": f"命中{len(evidence)}条知识规则", "data": evidence})
                if getattr(self.rag, "diagnostics", None):
                    output["rag_diagnostics"] = copy.deepcopy(self.rag.diagnostics)
            scene, risk = output["scene_analysis"], output["risk_result"]
            type_id = scene["type_id"]
            level = int(risk["risk_level"][0]) if risk else 0
            if level >= 4:
                type_id = "scam" if level == 5 else "scam_risk"
            elif level == 3:
                type_id = "telemarketing"
            call_type = CallType.from_id(type_id)
            summary = (output["transfer_result"].get("record_info") or output["business_result"].get("extract_info") or scene["summary"])
            card = {"title": f"{call_type.display_name} · {'高优先级提醒' if scene['urgency'] == 'high' else '来电记录'}",
                "body": summary, "priority": "high" if scene["urgency"] == "high" or level >= 4 else "normal",
                "data": {"处理建议": output["final_action"], "状态": owner["presence_mode"], "号码": number,
                    "风险依据": risk.get("reason", "由用户手动名单决定"), "说明": "来电信息为对方陈述；未实际接通、发短信或报警"}}
            output.update(call_id=call_id, created_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                caller_number=number, input_text=text, type_id=type_id, call_type_name=call_type.display_name,
                confidence=scene["confidence"], classify_method=method, presence_mode=owner["presence_mode"],
                presence_reason=owner["reason"], notification_card=card, rag_hint=rag_hint,
                rag_evidence=evidence, knowledge_sha256=knowledge_hash,
                elapsed_seconds=round(time.monotonic() - started, 3))
            # Check exportability before any persistence. Network/schema failures
            # above never turn into a successful profile judgment.
            json.dumps(output, ensure_ascii=False, allow_nan=False)
            output["caller_profile"] = profile
            save_errors = []
            try:
                output["caller_profile"] = self.profiles.record_call(number, type_id,
                    risk.get("risk_level", "unknown"), summary=summary, call_id=call_id)
            except (OSError, ValueError) as error:
                save_errors.append("号码历史保存失败：" + safe_error(error))
            try:
                self.notifications.append({"call_id": call_id, "created_at": output["created_at"], **card})
            except (OSError, ValueError) as error:
                save_errors.append("通知保存失败：" + safe_error(error))
            if save_errors:
                output["save_warning"] = "；".join(save_errors)
            try:
                self.history.append(output)
            except (OSError, ValueError) as error:
                save_errors.append("来电历史保存失败，请手动导出本次结果：" + safe_error(error))
                output["save_warning"] = "；".join(save_errors)
            return output

    def get_caller_profile(self, number):
        return self.profiles.get(number)

    def get_recent_history(self, limit=50):
        return self.history.recent(limit=limit)

    def _close_main_resources(self):
        if not self._resources_closed:
            self._resources_closed = True
            try:
                self.rag.close()
            finally:
                self._model_close()

    def close(self):
        self._close_main_resources()
        self.habit_learner.llm.close()
