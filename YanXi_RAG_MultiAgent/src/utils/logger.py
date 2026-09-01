"""
日志工具
========
统一日志配置，所有模块共用。
"""

import logging
import sys

# Windows 中文环境强制 UTF-8 输出
if sys.platform == "win32":
    try:
        import io
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
            sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        else:
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
            sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")
    except Exception:
        pass


def get_logger(name: str, level: str = "INFO") -> logging.Logger:
    """获取配置好的 logger"""
    logger = logging.getLogger(name)

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            datefmt="%H:%M:%S",
        ))
        logger.addHandler(handler)

    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    return logger


def setup_logger(name: str) -> logging.Logger:
    """setup_logger 别名（兼容 CC 项目风格）"""
    return get_logger(name)


def configure_logging(config: dict) -> None:
    """根据配置设置全局日志"""
    log_cfg = config.get("logging", {})
    level = log_cfg.get("level", "INFO")
    fmt = log_cfg.get("format", "%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    log_file = log_cfg.get("file")

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # 清空已有 handler
    root.handlers.clear()

    # 控制台 handler (使用支持 emoji 的编码)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter(fmt, datefmt="%H:%M:%S"))
    root.addHandler(console)

    # 文件 handler（基于项目根目录，不依赖 CWD）
    if log_file:
        from src.core.config import PROJECT_ROOT
        path = PROJECT_ROOT / log_file.lstrip("./")
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(str(path), encoding="utf-8")
        file_handler.setFormatter(logging.Formatter(fmt, datefmt="%Y-%m-%d %H:%M:%S"))
        root.addHandler(file_handler)
