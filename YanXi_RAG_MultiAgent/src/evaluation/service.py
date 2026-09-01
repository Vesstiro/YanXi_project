"""Sequential evaluation, stage errors and per-case SUT state isolation."""
import asyncio
import time
import uuid
from pathlib import Path

from src.desktop.audio import LocalTranscriber
from src.desktop.settings import ROOT
from .agents import ReviewAgent
from .models import RUBRIC_VERSION, write_json
from .settings import api_config, isolated_config


def code_fingerprint():
    import hashlib
    digest = hashlib.sha256()
    for path in sorted((ROOT / "src").rglob("*.py")) + [ROOT / "config.yaml"]:
        digest.update(str(path.relative_to(ROOT)).replace("\\", "/").encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def new_run_directory(root=ROOT):
    directory = Path(root) / "data" / "evaluation" / "runs" / (
        time.strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8])
    directory.mkdir(parents=True, exist_ok=False)
    return directory


class EvaluationService:
    def __init__(self, settings, directory):
        settings.validate()
        self.settings = settings
        self.directory = Path(directory)
        self.transcriber = LocalTranscriber(settings.stt_model, settings.stt_engine)
        self._judge_client = None

    @property
    def reviewer(self):
        if self._judge_client is None:
            from src.core.llm_client import LLMClient
            self._judge_client = LLMClient(api_config(self.settings, judge=True))
        return ReviewAgent(self._judge_client)

    def synthesize(self, case):
        import edge_tts
        directory = self.directory / "generated_audio"
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{case.id}.mp3"
        temporary = target.with_suffix(".mp3.part")

        async def save():
            await asyncio.wait_for(edge_tts.Communicate(case.reference, self.settings.voice).save(str(temporary)), 60)
        try:
            asyncio.run(save())
            if not temporary.is_file() or temporary.stat().st_size == 0:
                raise ValueError("语音合成未生成有效音频")
            temporary.replace(target)
            case.audio_path = str(target)
        finally:
            temporary.unlink(missing_ok=True)

    def run_assistant(self, case):
        from src.orchestration.orchestrator import CallOrchestrator
        from src.utils.presence import PresenceState, UserPresence
        config = isolated_config(self.settings, self.directory / "sut" / case.id)
        assistant = CallOrchestrator(config)
        clients = (assistant.llm_client, assistant.habit_learner.llm)
        # Bypass singleton construction only for the test fixture. Production code is untouched.
        presence = object.__new__(UserPresence)
        presence._state = PresenceState()
        assistant.presence = presence
        errors, calls = [], []
        for client in clients:
            original = client.chat

            def tracked(*args, _original=original, _client=client, **kwargs):
                started = time.monotonic()
                try:
                    result = _original(*args, **kwargs)
                    if _client.last_error:
                        errors.append(_client.last_error)
                    return result
                finally:
                    calls.append(round(time.monotonic() - started, 3))
            client.chat = tracked
        try:
            # Reference transcripts and generated expectations are never supplied here.
            output = assistant.run(case.transcript, caller_number="")
            if output.get("final_action") == "error":
                raise RuntimeError(output.get("final_message") or "管家运行失败")
            if not output or not output.get("final_action"):
                raise RuntimeError("管家返回空结果或缺少动作")
            case.timings["sut_api_calls"] = len(calls)
            case.timings["sut_api_seconds"] = round(sum(calls), 3)
            if errors:
                case.warnings.extend(self.settings.safe_error(e) for e in dict.fromkeys(errors))
                case.warnings.append("管家API异常后使用了兜底结果；此案例不纳入正常通过率。")
            return output, bool(errors)
        finally:
            for client in clients:
                client.close()

    def process(self, case, progress, allow_download=False):
        def stage(name, callback):
            case.error_stage = name
            progress(name)
            started = time.monotonic()
            try:
                return callback()
            finally:
                case.timings[name] = round(time.monotonic() - started, 3)

        if case.mode == "tts":
            if not case.reference:
                raise ValueError("没有可合成的来电台词")
            stage("语音合成", lambda: self.synthesize(case))
        if case.mode in ("audio", "tts"):
            case.transcript = stage("语音识别", lambda: self.transcriber.transcribe(
                case.audio_path, progress, allow_download=allow_download))
        elif case.mode in ("text", "reference"):
            case.error_stage = "读取文本"
            if not case.reference.strip():
                raise ValueError("该录音没有有效配套文本，请改用录音模式")
            case.transcript = case.reference.strip()
        else:
            raise ValueError("未知评测模式")
        if not case.transcript.strip():
            raise ValueError("转写为空，不能运行管家或评审")
        case.output, degraded = stage("AI管家", lambda: self.run_assistant(case))
        case.judgment = stage("独立评审", lambda: self.reviewer.judge(case))
        case.error_stage = ""
        return degraded

    def close(self):
        if self._judge_client:
            self._judge_client.close()
            self._judge_client = None


def run_evaluation(cases, service, stop, emit, allow_download=False):
    """Save every completed case. A save failure stops the run instead of losing results."""
    from .reports import save_report
    metadata = {"run_id": service.directory.name, "created_at": time.strftime("%Y-%m-%d %H:%M:%S%z"),
        "rubric_version": RUBRIC_VERSION, "settings": service.settings.public_dict(),
        "fixture": "独立单轮来电；空闲机主；无历史画像、习惯、白名单或额外授权",
        "stop_reason": "", "allow_model_download": allow_download, "code_sha256": code_fingerprint()}
    errors_in_row = 0
    try:
        # A rerun must not export an old verdict for an unexecuted case in this run.
        for case in cases:
            case.status, case.error, case.error_stage = "待评测", "", ""
            case.transcript, case.output, case.judgment, case.timings = "", {}, {}, {}
            case.elapsed = 0.0
            case.run_id = service.directory.name
            case.evaluation_config = {**metadata["settings"], "rubric_version": RUBRIC_VERSION,
                "code_sha256": metadata["code_sha256"]}
        save_report(service.directory, cases, metadata, full=False)
        for index, case in enumerate(cases, 1):
            if stop.is_set():
                metadata["stop_reason"] = "用户停止；未启动下一案例"
                break
            case.status, case.error, case.error_stage = "处理中", "", ""
            case.transcript, case.output, case.judgment, case.timings = "", {}, {}, {}
            case.warnings = [] if case.reference else ["无配套文本，须人工核听。"]
            started = time.monotonic()
            emit("case", case)
            try:
                degraded = service.process(case,
                    lambda message: emit("progress", f"{index}/{len(cases)} · {case.name} · {message}"), allow_download)
                case.status = "管家降级" if degraded else "已评审"
                errors_in_row = errors_in_row + 1 if degraded else 0
            except Exception as exc:
                case.status = "错误"
                case.error = service.settings.safe_error(exc)
                errors_in_row += 1
            case.elapsed = round(time.monotonic() - started, 2)
            write_json(service.directory / "cases" / (case.id + ".json"), case.export())
            save_report(service.directory, cases, metadata, full=False)
            emit("case", case)
            if errors_in_row >= 3:
                metadata["stop_reason"] = "连续3个案例发生运行错误或API降级，自动暂停，避免继续消耗额度"
                emit("notice", metadata["stop_reason"])
                break
        if stop.is_set() and not metadata["stop_reason"]:
            metadata["stop_reason"] = "用户停止；当前案例已完成"
    finally:
        metadata["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S%z")
        try:
            save_report(service.directory, cases, metadata, full=True)
        finally:
            service.close()
    return metadata


def test_connections(settings):
    from src.core.llm_client import LLMClient
    for judge in (False, True):
        client = LLMClient(api_config(settings, judge))
        try:
            response = client.chat(messages=[{"role": "user", "content": "只回复OK"}],
                temperature=0, max_tokens=16)
            if client.last_error or not response:
                raise RuntimeError(("评审" if judge else "管家") + "API：" + (client.last_error or "空响应"))
        finally:
            client.close()
