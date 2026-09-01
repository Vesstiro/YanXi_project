"""Strict, injectable four-agent pipeline derived from PythonProject2."""
import json
import math
import re
from .prompts import SCENE_PROMPT, RISK_PROMPT, BUSINESS_PROMPT, TRANSFER_PROMPT

TYPE_IDS = {"food_delivery", "express", "family", "friend", "leader", "colleague", "client",
            "bank", "interview", "taxi_arrived", "urgent", "general", "meaningless"}
REQUESTS = {"notify", "dropoff", "change_order", "payment", "refund", "schedule", "identity", "help", "other"}

def text_field(data, key, *, allow_empty=False, maximum=5000):
    value = data.get(key)
    if not isinstance(value, str) or len(value) > maximum or (not allow_empty and not value.strip()):
        raise ValueError(f"Agent字段 {key} 缺失或无效；本次不视为成功处理")
    return value.strip()

class JsonAgent:
    prompt = ""
    name = "Agent"

    def __init__(self, client):
        self.client = client

    def request(self, payload):
        response = self.client.chat(messages=[{"role": "system", "content": self.prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            temperature=0.15, max_tokens=2200, response_type="json")
        if self.client.last_error:
            raise RuntimeError(f"{self.name}未完成：{self.client.last_error}")
        if isinstance(response, str):
            response = re.sub(r"^```(?:json)?\s*|\s*```$", "", response.strip())
            try:
                response = json.loads(response)
            except ValueError as error:
                raise ValueError(f"{self.name}返回的不是有效JSON") from error
        if not isinstance(response, dict) or not response:
            raise ValueError(f"{self.name}返回空或非对象结果")
        json.dumps(response, ensure_ascii=False, allow_nan=False)
        return response

class DeepSceneAnalyzer(JsonAgent):
    prompt, name = SCENE_PROMPT, "深度场景Agent"

    def analyze(self, payload):
        data = self.request(payload)
        for key in ("scene_category", "summary", "speech_pattern", "info_completeness"):
            text_field(data, key)
        if data.get("type_id") not in TYPE_IDS or data.get("urgency") not in ("low", "medium", "high"):
            raise ValueError("场景类别或紧急度无效")
        if data.get("requested_action") not in REQUESTS:
            raise ValueError("场景requested_action无效")
        if type(data.get("needs_owner_decision")) is not bool or type(data.get("contradiction_flag")) is not bool:
            raise ValueError("场景判断标志必须是布尔值")
        if not isinstance(data.get("key_info"), dict) or any(not isinstance(v, str) for v in data["key_info"].values()):
            raise ValueError("场景key_info必须是文字键值对象")
        confidence = data.get("confidence")
        if type(confidence) not in (int, float) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise ValueError("场景confidence必须在0到1之间")
        return data

class RiskControlAgent(JsonAgent):
    prompt, name = RISK_PROMPT, "风险Agent"

    def assess(self, payload):
        data = self.request(payload)
        if data.get("risk_level") not in ("1级-安全", "2级-低风险", "3级-中风险", "4级-高风险", "5级-高危"):
            raise ValueError("风险等级必须为1至5级")
        if data.get("handle_suggestion") not in ("拦截", "拒绝", "转人工", "流转业务处理"):
            raise ValueError("风险处理建议无效")
        if type(data.get("risk_score")) not in (int, float) or not math.isfinite(data["risk_score"]) or not 0 <= data["risk_score"] <= 100:
            raise ValueError("风险分数必须在0到100之间")
        text_field(data, "reason")
        text_field(data, "risk_type")
        text_field(data, "reply_content", allow_empty=True)
        # No PythonProject2 _fix_risk: loan / price keywords never erase risk.
        return data

class BusinessProcessAgent(JsonAgent):
    prompt, name = BUSINESS_PROMPT, "业务Agent"

    def process(self, payload):
        data = self.request(payload)
        for key in ("business_type", "reply_content", "extract_info", "reason"):
            text_field(data, key)
        if data.get("handle_result") not in ("已处理", "需确认", "无法处理") or type(data.get("needs_owner_decision")) is not bool:
            raise ValueError("业务处理状态无效")
        return data

class ManualTransferAgent(JsonAgent):
    prompt, name = TRANSFER_PROMPT, "人工确认Agent"

    def transfer(self, payload):
        data = self.request(payload)
        if data.get("transfer_flag") is not True:
            raise ValueError("人工确认标志无效")
        for key in ("reply_content", "record_info", "reason"):
            text_field(data, key)
        return data
