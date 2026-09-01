"""Evaluator preferences, deliberately separate from the everyday assistant."""
import json
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from dotenv import dotenv_values, set_key, unset_key

from src.desktop.settings import DesktopSettings, ROOT, build_config, safe_error

DEFAULT_DATASET = ROOT / "test_audio_batch_200"


@dataclass
class EvaluationSettings:
    sut_model: str = "qwen-plus"
    judge_model: str = "qwen-plus"
    region: str = "中国内地"
    remember_key: bool = True
    stt_engine: str = "whisper"
    stt_model: str = "turbo"
    voice: str = "zh-CN-XiaoxiaoNeural"
    api_key: str = field(default="", repr=False)
    judge_api_key: str = field(default="", repr=False)

    def desktop(self, judge=False):
        return DesktopSettings(provider="qwen", model=self.judge_model if judge else self.sut_model,
            api_key=(self.judge_api_key or self.api_key) if judge else self.api_key,
            region=self.region, remember_key=self.remember_key, stt_engine=self.stt_engine,
            stt_model=self.stt_model, voice=self.voice)

    def validate(self):
        if not isinstance(self.judge_api_key, str):
            raise ValueError("评审 API Key 必须是文字")
        self.desktop().validate()
        self.desktop(True).validate()

    def public_dict(self):
        return {k: v for k, v in asdict(self).items() if k not in ("api_key", "judge_api_key")}

    def safe_error(self, error):
        message = str(error)
        for key in (self.api_key, self.judge_api_key):
            if key:
                message = message.replace(key, "[密钥已隐藏]")
        return safe_error(message)


class EvaluationSettingsStore:
    def __init__(self, root=ROOT):
        self.directory = Path(root) / "data" / "evaluation"
        self.path = self.directory / "settings.json"
        self.env_path = self.directory / ".env"

    def load(self):
        result = EvaluationSettings()
        try:
            if self.path.exists():
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("无效设置")
                result = EvaluationSettings(**{k: raw[k] for k in result.public_dict() if k in raw})
            # Never inherit a key from environment or the main GUI on first launch.
            values = dotenv_values(self.env_path) if self.env_path.exists() else {}
            if result.remember_key:
                result.api_key = values.get("EVALUATION_API_KEY") or ""
                result.judge_api_key = values.get("EVALUATION_JUDGE_API_KEY") or ""
            replace(result, api_key=result.api_key or "not-configured").validate()
            return result
        except (ValueError, TypeError):
            return EvaluationSettings()

    def save(self, settings):
        settings.validate()
        self.directory.mkdir(parents=True, exist_ok=True)
        for name, key in (("EVALUATION_API_KEY", settings.api_key),
                          ("EVALUATION_JUDGE_API_KEY", settings.judge_api_key)):
            if settings.remember_key and key.strip():
                set_key(str(self.env_path), name, key.strip(), encoding="utf-8")
            elif self.env_path.exists() and name in dotenv_values(self.env_path):
                unset_key(str(self.env_path), name, encoding="utf-8")
        from .models import write_json
        write_json(self.path, settings.public_dict())


def api_config(settings, judge=False):
    config = build_config(settings.desktop(judge))
    config["llm"]["qwen"]["timeout"] = 60 if judge else 30
    return config


def isolated_config(settings, directory):
    """A fresh config per case; never point to the user's daily profiles/logs."""
    config = api_config(settings)
    directory = Path(directory).resolve()
    for section, key, relative in (
        ("caller_profile", "persist_path", "profiles.json"),
        ("habit", "persist_path", "habits.json"),
        ("notification", "persist_path", "notifications"),
        ("call_log", "persist_dir", "call_logs"),
        ("recorder", "persist_dir", "recordings"),
        ("rag", "chroma_persist_dir", "chroma"),
        ("logging", "file", "runtime.log"),
    ):
        config.setdefault(section, {})[key] = str(directory / relative)
    return config
