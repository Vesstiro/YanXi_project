"""Scene -> risk -> business / human handoff, preserving PythonProject2's topology."""
from .agents import DeepSceneAnalyzer, RiskControlAgent, BusinessProcessAgent, ManualTransferAgent
from .policy import route, reject_reply, guard_reply

class CallAgentScheduler:
    def __init__(self, client):
        self.scene_analyzer = DeepSceneAnalyzer(client)
        self.risk_agent = RiskControlAgent(client)
        self.business_agent = BusinessProcessAgent(client)
        self.transfer_agent = ManualTransferAgent(client)

    def handle(self, call_text, rag_hint, owner, profile):
        trace = []
        # Only necessary profile facts enter API requests, never complete call history.
        caller_context = {key: profile.get(key) for key in ("phone_number", "display_name", "list_status",
            "trust_score", "call_count", "trust_description")}
        payload = {"caller_text": call_text, "rag_reference_examples": rag_hint,
                   "owner_context": owner, "local_number_history_not_identity_proof": caller_context}
        scene = self.scene_analyzer.analyze(payload)
        trace.append({"step": "深度场景分析", "content": scene["summary"], "data": scene})
        payload["scene_analysis"] = scene
        risk = self.risk_agent.assess(payload)
        trace.append({"step": "风险防控", "content": risk["reason"], "data": risk})
        payload["risk_result"] = risk
        policy = route(scene, risk, owner, profile, call_text)
        business, transfer, guard_notes = {}, {}, []
        payload["route_policy"] = policy
        if policy["action"] == "reject":
            reply = reject_reply(risk, profile.get("list_status") == "blacklist")
        elif policy["needs_owner"] or policy["action"] == "forward":
            transfer = self.transfer_agent.transfer(payload)
            reply = transfer["reply_content"]
            trace.append({"step": "人工确认与留言", "content": transfer["reason"], "data": transfer})
        else:
            business = self.business_agent.process(payload)
            trace.append({"step": "业务处理", "content": business["reason"], "data": business})
            if business["needs_owner_decision"] or business["handle_result"] != "已处理":
                policy = route(scene, risk, owner, profile, call_text, needs_owner=True)
                payload.update(route_policy=policy, business_result=business)
                transfer = self.transfer_agent.transfer(payload)
                trace.append({"step": "业务升级人工确认", "content": transfer["reason"], "data": transfer})
                reply = transfer["reply_content"]
            else:
                reply = business["reply_content"]
        if policy["action"] != "reject":
            reply, guard_notes = guard_reply(reply, scene, policy, owner, call_text)
        trace.append({"step": "最终权限与状态策略", "content": policy["reason"], "data": policy})
        if guard_notes:
            trace.append({"step": "输出边界检查", "content": "；".join(guard_notes)})
        return {"scene_analysis": scene, "risk_result": risk, "business_result": business,
                "transfer_result": transfer, "agent_reply": reply, "final_action": policy["action"],
                "route_policy": policy, "policy_notes": guard_notes, "agent_trace": trace}
