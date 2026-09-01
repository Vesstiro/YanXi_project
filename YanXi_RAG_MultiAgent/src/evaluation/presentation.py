"""Read-only result views. Formatting never calls a model or changes a verdict."""
import json
import math

from .models import DIMENSIONS, MODES, VERDICTS
from .reports import DISCLAIMER

RESULT_TABS = {
    "reply": "直接回复",
    "evaluation": "评测评价",
    "evidence": "评分依据",
    "process": "判断过程",
    "input": "原文/转写",
    "runtime": "运行详情",
    "raw": "原始数据",
}
PROCESS_NOTICE = "这里展示程序记录的检索结果、Agent结构化分析和决策依据，不是模型未公开的隐藏思维链。"
FIELD_NAMES = {
    "scene_category": "场景类别", "type_id": "类别标识", "urgency": "紧急程度",
    "summary": "摘要", "speech_pattern": "话术模式", "key_info": "关键信息",
    "contradiction_flag": "是否有矛盾", "contradiction_detail": "矛盾说明",
    "info_completeness": "信息完整度", "requested_action": "来电诉求",
    "needs_owner_decision": "是否需机主决定", "confidence": "置信度",
    "risk_level": "风险等级", "risk_type": "风险类型", "risk_score": "风险分",
    "reason": "判断依据", "handle_suggestion": "处理建议", "reply_content": "本阶段候选回复",
    "business_type": "业务类型", "handle_result": "处理结果", "extract_info": "信息提取",
    "transfer_flag": "需要人工确认", "record_info": "记录内容", "action": "最终动作",
    "needs_owner": "是否需机主确认", "emergency": "是否紧急",
    "limited_meal_change": "是否启用有限改餐授权", "notes": "说明",
    "content": "内容", "type": "类型", "rule": "参考规则", "score": "匹配分", "source": "来源",
}

def _text(value, default="—"):
    if value is None or value == "":
        return default
    return str(value)

def _percent(value):
    return f"{value:.0%}" if type(value) in (int, float) and math.isfinite(value) else "—"

def _json(value):
    return json.dumps(value, ensure_ascii=False, indent=2)

def _structure(value, indent=0):
    """Readable labels for known fields; unknown data is still shown verbatim."""
    prefix = "  " * indent
    if isinstance(value, dict):
        lines = []
        for key, item in value.items():
            label = FIELD_NAMES.get(key, key)
            if isinstance(item, (dict, list)):
                lines.extend((f"{prefix}{label}：", _structure(item, indent + 1)))
            else:
                shown = ("是" if item else "否") if isinstance(item, bool) else _text(item)
                lines.append(f"{prefix}{label}：{shown}")
        return "\n".join(lines) or prefix + "（无）"
    if isinstance(value, list):
        return "\n".join(f"{prefix}{i}. {_structure(item, indent + 1).lstrip()}"
            for i, item in enumerate(value, 1)) or prefix + "（无）"
    return prefix + _text(value)

def _reply_view(case):
    output = case.output
    # Older reports may use PythonProject2's final_reply field. Never substitute
    # the reviewer's improved_reply for the assistant's actual reply.
    for key in ("agent_reply", "final_reply"):
        reply = output.get(key)
        if isinstance(reply, str) and reply.strip():
            return reply
    if output.get("final_action") == "reject":
        return "本条为拒接／拦截建议，没有口头回复。可在“判断过程”和“原始数据”查看依据。"
    if output:
        return "本条记录没有保存管家的直接回复。不会用评测AI的建议回复替代原回复。"
    return "尚未生成管家回复。" + ("请在“运行详情”查看错误。" if case.error else "")

def _evaluation_view(case):
    j = case.judgment
    if not j:
        return "尚无独立评测AI的评价。" + ("本条发生运行异常，请查看“运行详情”。" if case.error else "评审完成后会在此显示结论。")
    lines = []
    if case.status != "已评审":
        lines.append(f"运行状态：{case.status}。以下为已保存的评审意见，不代表正常通过。\n")
    lines += [f"最终判定：{VERDICTS.get(j.get('verdict'), '—')}",
        f"总分：{_text(j.get('score'))}/100    评审置信度：{_percent(j.get('confidence'))}",
        "", "【评测AI评价】", _text(j.get("summary")),
        "", "【合理的处理方式】", _text(j.get("expected_behavior")),
        "", "【评测AI建议回复 · 不是管家原回复】", _text(j.get("improved_reply")),
        "", "【风险提示】", "\n".join("• " + str(v) for v in j.get("risks", [])) or "评审未列出额外风险。",
        "", "逐项得分、引用证据、ASR归因及最终判定校验，请切换“评分依据”。",
        "", "模型评价是辅助意见；不确定、高风险及紧急案例仍需人工复核。"]
    return "\n".join(lines)

def _evidence_view(case):
    j = case.judgment
    if not j:
        return "尚无评分依据。评审完成后，这里分项显示四维得分、引用证据和判断原因。"
    lines = []
    for key, label in DIMENSIONS.items():
        part = j.get("dimensions", {}).get(key, {})
        lines += [f"【{label}】  {_text(part.get('score'))}/5", "引用证据：" + _text(part.get("evidence")),
            "判断原因：" + _text(part.get("reason")), ""]
    asr = j.get("asr", {})
    impact = {"none": "未见影响", "minor": "轻微差异", "material": "实质改变原意", "unknown": "无法核实"}
    cause = {"sut": "管家逻辑", "asr": "语音识别", "both": "管家与语音识别", "none": "未发现问题", "uncertain": "尚不确定"}
    lines += ["【ASR与问题归因】", "ASR影响：" + impact.get(asr.get("impact"), _text(asr.get("impact"))),
        "依据：" + _text(asr.get("reason")), "归因：" + cause.get(j.get("root_cause"), _text(j.get("root_cause"))),
        "", "【模型原判与最终校验】", "模型原判：" + VERDICTS.get(j.get("model_verdict"), "—"),
        "最终判定：" + VERDICTS.get(j.get("verdict"), "—"),
        *(j.get("policy_notes") or ["没有触发额外判定修正规则。"])]
    return "\n".join(lines)

def _process_view(case):
    output = case.output
    lines = [PROCESS_NOTICE, ""]
    trace = output.get("agent_trace")
    if isinstance(trace, list) and trace:
        for index, entry in enumerate(trace, 1):
            if isinstance(entry, dict):
                lines += [f"【步骤{index} · {_text(entry.get('step'), '处理步骤')}】", _text(entry.get("content"))]
                if entry.get("data"):
                    lines += [_structure(entry["data"])]
            else:
                lines += [f"【步骤{index}】", str(entry)]
            lines.append("")
    else:
        recorded = False
        for key, label in (("scene_analysis", "场景分析"), ("risk_result", "风险判断"),
                           ("business_result", "业务处理"), ("transfer_result", "人工确认"),
                           ("route_policy", "最终路由与权限")):
            if output.get(key):
                recorded = True
                lines += ["【" + label + "】", _structure(output[key]), ""]
        if not recorded:
            lines += ["该案例尚未保存逐步Agent分析；旧报告也可能不包含这些字段。不会根据最终回复倒推或编造思考过程。", ""]
    if output.get("rag_hint"):
        lines += ["【交给Agent的RAG参考】", _text(output["rag_hint"]), ""]
    if output.get("policy_notes"):
        lines += ["【管家输出边界检查】", _structure(output["policy_notes"]), ""]
    if output.get("notification_card"):
        lines += ["【最终通知卡片】", _structure(output["notification_card"])]
    return "\n".join(lines)

def result_views(case):
    """Return seven separate strings without mutating the case or rerunning work."""
    inputs = ["【配套原文 / 合成来电】", case.reference or "无配套原文，需要人工核听。",
        "", "【实际传给管家的文本】", case.transcript or "尚未识别或处理。",
        "", "输入模式：" + MODES.get(case.mode, case.mode), "来源：" + case.source,
        "音频文件：" + (case.audio_path or "无"), "参考文本文件：" + (case.reference_path or "无")]
    runtime = ["案例：" + case.name, "状态：" + case.status, f"耗时：{case.elapsed:.1f}秒",
        "运行ID：" + (case.run_id or "尚未运行"), "", "【运行错误】",
        f"{case.error_stage or '运行阶段'}：{case.error}" if case.error else "无已记录错误。",
        "", "【警告与保存提示】", *(case.warnings or [])]
    runtime += [str(case.output[k]) for k in ("desktop_warning", "save_warning") if case.output.get(k)]
    if not case.warnings and not any(case.output.get(k) for k in ("desktop_warning", "save_warning")):
        runtime.append("无已记录提示。")
    runtime += ["", "【分阶段耗时 / 对话调用统计】", _structure(case.timings),
        "", "【RAG诊断与知识版本】", _structure(case.output.get("rag_diagnostics", {})),
        "知识SHA256：" + _text(case.output.get("knowledge_sha256"))]
    if case.novelty or case.expected_behavior:
        runtime += ["", "【新增场景去重依据】", _structure(case.novelty),
            "生成器预期（不是标准答案）：" + _text(case.expected_behavior)]
    runtime += ["", "【本案例评测配置】", _structure(case.evaluation_config), "", DISCLAIMER]
    return {"reply": _reply_view(case), "evaluation": _evaluation_view(case),
        "evidence": _evidence_view(case), "process": _process_view(case),
        "input": "\n".join(inputs), "runtime": "\n".join(runtime), "raw": _json(case.export())}
