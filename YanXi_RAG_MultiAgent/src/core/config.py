"""
配置加载与校验
=============
统一加载 config.yaml，合并默认值，校验必填项。
"""

from pathlib import Path
import yaml
from dotenv import load_dotenv

from src.utils.logger import get_logger

logger = get_logger(__name__)

# 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# 加载本地密钥配置；默认不覆盖用户已设置的系统环境变量。
load_dotenv(PROJECT_ROOT / ".env", override=False)

# 默认配置
DEFAULT_CONFIG = {
    "llm": {
        "backend": "qwen",
        "deepseek": {
            "api_key_env": "DEEPSEEK_API_KEY",
            "base_url": "https://api.deepseek.com/v1",
            "model": "deepseek-chat",
            "temperature": 0.3,
            "max_tokens": 2048,
            "timeout": 30,
        },
        "zhipu": {
            "api_key_env": "ZHIPU_API_KEY",
            "model": "glm-4-flash",
            "temperature": 0.3,
            "max_tokens": 2048,
            "timeout": 30,
        },
        "qwen": {
            "api_key_env": "QWEN_API_KEY",
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "model": "qwen-plus",
            "temperature": 0.3,
            "max_tokens": 2048,
            "timeout": 30,
        },
        "fallback_chain": ["qwen", "deepseek", "zhipu"],
        "retry": {"max_retries": 3, "base_delay": 1.0},
    },
    "rag": {
        "knowledge_path": "./assets/knowledge.txt",
        "embedding_model": "text-embedding-v4",
        "embedding_dimensions": 1024,
        "embedding_device": "cpu",
        "chroma_persist_dir": "./data/chroma_db",
        "collection_name": "yanxi_knowledge",
        "chunk_size": 512,
        "chunk_overlap": 50,
        "retrieval_top_k": 5,
    },
    "hybrid_retrieval": {
        "bm25_weight": 0.4,
        "vector_weight": 0.6,
        "rrf_k": 60,
        "reranker_semantic_weight": 0.5,
        "reranker_keyword_weight": 0.3,
    },
    "orchestrator": {
        "max_conversation_rounds": 10,
        "scam_confidence_threshold": 0.7,
    },
    "habit": {"persist_path": "./data/habits/habit_store.json", "auto_detect": False},
    "notification": {"persist_path": "./data/notifications"},
    "call_log": {"persist_dir": "./data/call_logs"},
    "caller_profile": {"persist_path": "./data/profiles/caller_profiles.json"},
    "conversation_memory": {"max_short_term": 50, "context_window": 10},
    "logging": {
        "level": "INFO",
        "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        "file": "./data/yanxi.log",
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """深度合并两个字典"""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(config_path: str | None = None) -> dict:
    """
    加载配置文件，与默认值合并。

    参数:
        config_path: 配置文件路径（默认 config.yaml）

    返回:
        dict: 合并后的配置
    """
    if config_path is None:
        config_path = str(PROJECT_ROOT / "config.yaml")

    config_path_obj = Path(config_path)
    if not config_path_obj.is_absolute():
        config_path_obj = PROJECT_ROOT / config_path_obj

    config = DEFAULT_CONFIG.copy()

    if config_path_obj.exists():
        with open(config_path_obj, "r", encoding="utf-8") as f:
            user_config = yaml.safe_load(f) or {}
        config = _deep_merge(config, user_config)
        logger.info(f"配置加载完成: {config_path_obj}")
    else:
        logger.warning(f"配置文件不存在: {config_path_obj}，使用默认配置")

    return config


def load_and_validate_config(config_path: str | None = None) -> dict:
    """
    加载并校验配置（兼容 CC 项目接口）。

    参数:
        config_path: 配置文件路径

    返回:
        dict: 合并后的配置
    """
    return load_config(config_path)


def resolve_path(path_str: str) -> Path:
    """将配置中的相对路径解析为基于项目根目录的绝对路径

    参数:
        path_str: 相对路径（如 "./data/chroma_db"）或绝对路径

    返回:
        Path: 绝对路径
    """
    path = Path(path_str)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path
