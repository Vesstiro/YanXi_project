"""GUI-independent processing and a sequential, failure-isolated batch runner."""
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from src.desktop.audio import LocalTranscriber
from src.desktop.settings import ROOT, build_config, safe_error

ACTION_NAMES = {"reject": "拒接 / 拦截", "forward": "建议转接机主", "proxy": "AI 代接",
    "record": "记录留言", "ask": "询问信息", "general_reply": "普通回复",
    "summary_card": "生成摘要卡片", "continue_conversation": "需要继续对话", "error": "处理失败"}


@dataclass
class Job:
    source: str
    kind: str = "audio"
    text: str = ""
    caller_number: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: str = "待处理"
    transcript: str = ""
    result: dict = field(default_factory=dict)
    error: str = ""
    elapsed: float = 0.0

    def export(self):
        return asdict(self)


def readable_result(result):
    if not result:
        return "处理完成后，这里显示来电类型、动作、回复和通知摘要。"
    card = result.get("notification_card") or {}
    confidence = result.get("confidence", 0)
    lines = [f"来电类型：{result.get('call_type_name', result.get('type_id', '未知'))}",
             f"置信度：{confidence:.0%}    判断方式：{result.get('classify_method', '未知')}",
             f"处理动作：{ACTION_NAMES.get(result.get('final_action'), result.get('final_action', '未知'))}",
             "", "AI 回复", result.get("agent_reply") or "（无口头回复）"]
    if card:
        lines += ["", "通知摘要", card.get("title", ""), card.get("body", "")]
        if card.get("data"):
            lines.append(json.dumps(card["data"], ensure_ascii=False, indent=2))
    if result.get("desktop_warning"):
        lines += ["", "提示：" + result["desktop_warning"]]
    if result.get("save_warning"):
        lines += ["", "保存提示：" + result["save_warning"]]
    if result.get("caller_number"):
        lines += ["", "来电号码：" + str(result["caller_number"])]
    if result.get("caller_profile"):
        profile = result["caller_profile"]
        lines += ["号码行为信任参考：" + str(profile.get("trust_score", "未建立")) + " / 100",
                  "联系人标签与行为评分不代表来电者身份已验证。"]
    if result.get("presence_mode"):
        labels = {"free": "空闲", "busy": "忙碌", "dnd": "免打扰", "driving": "驾车"}
        lines += ["本次机主状态：" + labels.get(result["presence_mode"], str(result["presence_mode"]))]
    lines += ["", "以上动作是系统的处理建议，不会真实接听、拒接或拨打电话。"]
    return "\n".join(lines)


def reasoning_details(result):
    """Show observable retrieval/agent outputs, not a claimed hidden reasoning process."""
    if not result:
        return "处理后显示知识检索命中、各 Agent 的结构化判断及流程轨迹。"
    lines = ["【RAG 检索提示】", str(result.get("rag_hint") or result.get("rag_context") or "无检索提示"),
             "", "【知识命中证据】"]
    evidence = result.get("rag_evidence") or result.get("retrieval_results") or []
    lines.append(json.dumps(evidence, ensure_ascii=False, indent=2) if evidence else "无命中证据；请查看错误或降级提示。")
    if result.get("knowledge_sha256"):
        lines += ["", "语料指纹：" + str(result["knowledge_sha256"])]
    for key, label in (("scene_analysis", "场景分析"), ("risk_result", "风险 Agent"),
                       ("business_result", "业务 Agent"), ("transfer_result", "转接 Agent"),
                       ("agent_trace", "Agent 执行轨迹")):
        if result.get(key):
            lines += ["", "【" + label + "】", json.dumps(result[key], ensure_ascii=False, indent=2)]
    lines += ["", "此页展示检索结果与程序公开输出，不代表身份认证，也不是模型的隐藏思维链。"]
    return "\n".join(lines)


def speech_text(result, full=False):
    card = result.get("notification_card") or {}
    reply = result.get("agent_reply", "").strip()
    if reply and not full:
        return reply
    parts = [f"来电类型：{result.get('call_type_name', '未知')}。",
             f"处理建议：{ACTION_NAMES.get(result.get('final_action'), '请查看结果')}。",
             reply, card.get("title", ""), card.get("body", "")]
    return "\n".join(str(part) for part in parts if part)


class DesktopService:
    def __init__(self, settings, root=ROOT):
        self.settings = settings
        self.root = Path(root)
        self.transcriber = LocalTranscriber(settings.stt_model, settings.stt_engine)
        self.orchestrator = None

    def test_connection(self):
        from src.core.llm_client import LLMClient
        client = LLMClient(build_config(self.settings, root=self.root))
        try:
            reply = client.chat([{"role": "user", "content": "只回复OK"}], max_tokens=16, temperature=0)
            if not reply or not str(reply).strip():
                raise RuntimeError(client.last_error or "未收到模型回复。请检查 API Key、服务区域、模型名称和账户额度。")
            return "对话接口连接成功。首次处理来电还会用同一 Key 调用 Qwen 嵌入接口，检索权限以实际运行结果为准。"
        finally:
            client.close()

    def process(self, job, progress, allow_download=False):
        text = job.text if job.kind == "text" else self.transcriber.transcribe(job.source, progress, allow_download)
        job.transcript = text.strip()
        if not job.transcript:
            raise ValueError("输入文字为空。")
        progress("分析来电内容")
        # This is the only daily-state injection point. EvaluationService never uses it.
        from src.context.stores import OwnerSettingsStore
        if self.orchestrator is None:
            from src.orchestration.orchestrator import CallOrchestrator
            config = build_config(self.settings, include_owner=True, root=self.root)
            owner = config["owner"]
            self.orchestrator = CallOrchestrator(config)
        else:
            owner = OwnerSettingsStore(root=self.root).load()
        if hasattr(self.orchestrator, "set_owner_context"):
            self.orchestrator.set_owner_context(owner)
        self.orchestrator.llm_client.last_error = None
        result = self.orchestrator.run(job.transcript, caller_number=job.caller_number)
        if result.get("final_action") == "error":
            raise RuntimeError(result.get("final_message") or "来电处理失败")
        if getattr(self.orchestrator.llm_client, "last_error", None):
            result["desktop_warning"] = self.orchestrator.llm_client.last_error + "。本条使用规则或模板降级处理。"
        return result

    def close(self):
        if self.orchestrator is not None:
            if callable(getattr(self.orchestrator, "close", None)):
                self.orchestrator.close()
            else:
                clients = (self.orchestrator.llm_client, self.orchestrator.habit_learner.llm)
                seen = set()
                for client in clients:
                    if id(client) not in seen:
                        client.close()
                        seen.add(id(client))
        self.orchestrator = None


def run_batch(jobs, service, stop, emit, allow_download=False, output_dir=None):
    """Stop means finish the current job, leave remaining jobs pending for resume."""
    try:
        for index, job in enumerate(jobs):
            if stop.is_set():
                break
            job.error, job.result, job.elapsed = "", {}, 0.0
            job.transcript = ""
            job.status = "处理中"
            emit("job", job.id)
            started = time.monotonic()
            try:
                def progress(message):
                    emit("progress", (job.id, message, index + 1, len(jobs)))
                job.result = service.process(job, progress, allow_download)
                job.status = "降级完成" if job.result.get("desktop_warning") else "完成"
            except Exception as error:
                job.status = "失败"
                job.error = safe_error(error, service.settings.api_key)
            job.elapsed = round(time.monotonic() - started, 2)
            if output_dir is not None:
                try:
                    directory = Path(output_dir)
                    directory.mkdir(parents=True, exist_ok=True)
                    path = directory / f"{job.id}.json"
                    temp = path.with_suffix(".tmp")
                    temp.write_text(json.dumps(job.export(), ensure_ascii=False, indent=2), encoding="utf-8")
                    temp.replace(path)
                except OSError as error:
                    job.result["save_warning"] = "自动保存失败，请手动导出。" + safe_error(error)
                    emit("notice", "结果已生成，但自动保存失败：" + safe_error(error))
            emit("job", job.id)
    finally:
        emit("batch_done", stop.is_set())
