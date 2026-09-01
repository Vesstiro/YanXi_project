"""Explicit offline doubles, never imported by production entry points."""
import copy

def scene(**changes):
    result = {"scene_category": "外卖配送", "type_id": "food_delivery", "urgency": "low",
        "speech_pattern": "正常沟通", "summary": "外卖已放教学楼门口", "key_info": {"地点": "教学楼门口"},
        "contradiction_flag": False, "contradiction_detail": "", "info_completeness": "完整",
        "requested_action": "notify", "needs_owner_decision": False, "confidence": 0.9}
    result.update(changes)
    return result

def risk(level=1):
    labels = {1: "1级-安全", 2: "2级-低风险", 3: "3级-中风险", 4: "4级-高风险", 5: "5级-高危"}
    return {"risk_level": labels[level], "risk_type": "正常外卖" if level == 1 else "待核实的风险",
        "handle_suggestion": "流转业务处理" if level == 1 else "转人工" if level == 2 else "拒绝" if level == 3 else "拦截",
        "risk_score": (level - 1) * 20, "reason": "测试替身提供的明确业务依据", "reply_content": ""}

def business(reply="收到，外卖已放教学楼门口的信息已记录，谢谢告知。", **changes):
    result = {"business_type": "外卖", "handle_result": "已处理", "reply_content": reply,
        "extract_info": "外卖已放教学楼门口", "needs_owner_decision": False, "reason": "确认已明确的送达信息"}
    result.update(changes)
    return result

def transfer(reply="您的诉求已记录，这件事需要机主本人确认。"):
    return {"transfer_flag": True, "reply_content": reply, "record_info": "需要机主确认来电诉求", "reason": "没有代决策授权"}

class FakeClient:
    def __init__(self, config=None, responses=None):
        self.last_error = None
        self.responses = list(responses if responses is not None else [scene(), risk(), business()])
        self.requests, self.closed = [], False

    def chat(self, *args, **kwargs):
        self.requests.append(copy.deepcopy(kwargs))
        if not self.responses:
            raise AssertionError("Unexpected extra model request in offline test")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            self.last_error = "模拟API错误"
            return {}
        return copy.deepcopy(item)

    def close(self):
        self.closed = True

class FakeRag:
    def __init__(self, *args, **kwargs):
        self.last_evidence = [{"content": "外卖已放门口", "type": "外卖", "rule": "只记录已说明的位置",
                               "score": 0.8, "source": "ngram+vector"}]
        self.knowledge_sha256 = "a" * 64
        self.diagnostics = []
        self.queries = []
        self.closed = False

    def search(self, text):
        self.queries.append(text)
        return "匹配规则1：外卖已放门口；只记录已说明的位置"

    def close(self):
        self.closed = True
