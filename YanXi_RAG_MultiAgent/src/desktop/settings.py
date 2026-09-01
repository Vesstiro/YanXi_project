"""Non-secret desktop preferences and explicitly opted-in local credentials."""
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from dotenv import dotenv_values, set_key, unset_key

ROOT = Path(__file__).resolve().parents[2]
PROVIDERS = {"qwen": "通义千问 Qwen", "deepseek": "DeepSeek", "zhipu": "智谱 GLM"}
KEY_NAMES = {"qwen": "QWEN_API_KEY", "deepseek": "DEEPSEEK_API_KEY", "zhipu": "ZHIPU_API_KEY"}
DEFAULT_MODELS = {"qwen": "qwen-plus", "deepseek": "deepseek-chat", "zhipu": "glm-4-flash"}
VOICES = {"晓晓 · 女声": "zh-CN-XiaoxiaoNeural", "云希 · 男声": "zh-CN-YunxiNeural",
          "晓伊 · 女声": "zh-CN-XiaoyiNeural", "云健 · 男声": "zh-CN-YunjianNeural"}
STT_MODELS = ("tiny", "base", "small", "medium", "large-v3", "turbo")


@dataclass
class DesktopSettings:
    provider: str = "qwen"
    model: str = "qwen-plus"
    region: str = "中国内地"
    remember_key: bool = True
    stt_engine: str = "whisper"
    stt_model: str = "turbo"
    voice: str = "zh-CN-XiaoxiaoNeural"
    api_key: str = field(default="", repr=False)

    def public_dict(self):
        return {k: v for k, v in asdict(self).items() if k != "api_key"}

    def validate(self):
        for name in ("provider", "model", "region", "api_key", "stt_engine", "stt_model", "voice"):
            if not isinstance(getattr(self, name), str):
                raise ValueError(f"设置字段 {name} 必须是文字。")
        if not isinstance(self.remember_key, bool):
            raise ValueError("保存密钥选项必须为布尔值。")
        if self.provider not in PROVIDERS:
            raise ValueError("请选择支持的模型服务。")
        if not self.api_key.strip():
            raise ValueError("请输入 API Key。")
        if any(ch.isspace() for ch in self.api_key.strip()):
            raise ValueError("API Key 不能包含空白或换行。")
        if not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,120}", self.model.strip()):
            raise ValueError("模型名称只能包含字母、数字、点、斜杠、下划线和短横线。")
        if self.region not in ("中国内地", "国际 / 新加坡"):
            raise ValueError("请选择正确的服务区域。")
        if self.stt_engine not in ("whisper", "faster-whisper"):
            raise ValueError("请选择有效的语音识别引擎。")
        if self.stt_model not in STT_MODELS or self.voice not in VOICES.values():
            raise ValueError("请选择有效的识别模型和播报音色。")


class SettingsStore:
    def __init__(self, root=ROOT):
        self.root = Path(root)
        self.env_path = self.root / ".env"
        self.path = self.root / "data" / "desktop" / "settings.json"

    def saved_key(self, provider):
        name = KEY_NAMES[provider]
        local = dotenv_values(self.env_path) if self.env_path.exists() else {}
        return str(local.get(name) or os.environ.get(name, ""))

    def load(self):
        settings = DesktopSettings()
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("设置必须是 JSON 对象")
                for key in settings.public_dict():
                    if key in raw:
                        setattr(settings, key, raw[key])
                settings.api_key = self.saved_key(settings.provider)
                # Validate choices even when first-launch key is absent.
                from dataclasses import replace
                replace(settings, api_key=settings.api_key or "not-configured").validate()
                return settings
            except (ValueError, TypeError, KeyError):
                # Preserve the original file. A later explicit save replaces it.
                settings = DesktopSettings()
        settings.api_key = self.saved_key(settings.provider)
        return settings

    def save(self, settings):
        settings.validate()
        name = KEY_NAMES[settings.provider]
        key = settings.api_key.strip()
        self.root.mkdir(parents=True, exist_ok=True)
        if settings.remember_key:
            set_key(str(self.env_path), name, key, quote_mode="always", encoding="utf-8")
        elif self.env_path.exists():
            # The dialog explicitly explains that unchecking removes this provider's saved key.
            unset_key(str(self.env_path), name, encoding="utf-8")
        os.environ[name] = key
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(settings.public_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.path)


def build_config(settings, *, include_owner=False, root=None):
    """Build API paths; daily owner state is an explicit desktop-only opt-in.

    The unchanged evaluator calls this function without ``include_owner``. Never
    let evaluation cases inherit real contacts or delivery/order authorizations.
    """
    from copy import deepcopy
    from src.core.config import load_config
    project_root = Path(root) if root is not None else ROOT
    settings.validate()
    config = deepcopy(load_config())
    llm = config["llm"]
    llm["backend"] = settings.provider
    llm["fallback_chain"] = [settings.provider]
    llm["retry"] = {"max_retries": 1, "base_delay": 1.0}
    for provider in PROVIDERS:
        llm[provider]["api_key"] = settings.api_key.strip() if provider == settings.provider else ""
        llm[provider]["api_key_env"] = ""
    llm[settings.provider]["model"] = settings.model.strip()
    llm[settings.provider]["timeout"] = 30
    llm["qwen"]["base_url"] = ("https://dashscope.aliyuncs.com/compatible-mode/v1"
        if settings.region == "中国内地" else "https://dashscope-intl.aliyuncs.com/compatible-mode/v1")
    # Resolve runtime data independently of the launch working directory.
    for section, key in (("caller_profile", "persist_path"), ("habit", "persist_path"),
                         ("notification", "persist_path"), ("call_log", "persist_dir"),
                         ("recorder", "persist_dir")):
        value = config.get(section, {}).get(key)
        if value and not Path(value).is_absolute():
            config[section][key] = str(project_root / value)
    rag = config.setdefault("rag", {})
    rag["knowledge_path"] = str(project_root / "assets" / "knowledge.txt")
    persist = Path(rag.get("chroma_persist_dir", "data/chroma_db"))
    rag["chroma_persist_dir"] = str(persist if persist.is_absolute() else project_root / persist)
    config.pop("owner", None)
    if include_owner:
        from src.context.stores import OwnerSettingsStore
        config["owner"] = OwnerSettingsStore(root=project_root).load()
    return config


def safe_error(error, key=""):
    text = str(error)
    if key:
        text = text.replace(key, "[密钥已隐藏]")
    return re.sub(r"sk-[A-Za-z0-9_-]+", "[密钥已隐藏]", text)[:700]
