"""日志：尝试读 config.yml 控制级别和输出文件；读不到就退化到默认配置（控制台输出）。

设计目的是让 `import core` 这类探索操作不需要先准备 config.yml。
"""
import os
import sys

import yaml
from loguru import logger


_DEFAULT_FORMAT_CONSOLE = (
    "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
    "<level>{message}</level>"
)
_DEFAULT_FORMAT_FILE = (
    "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}"
)

_configured = False


def _load_config():
    """读 config.yml；不存在/损坏时返回 {} 而不是抛异常。"""
    try:
        with open("config.yml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except (FileNotFoundError, OSError, yaml.YAMLError):
        return {}


def _configure_once(level: str):
    global _configured
    if _configured:
        return
    _configured = True

    config = _load_config()
    log_cfg = (config.get("logging") or {}) if isinstance(config, dict) else {}
    effective_level = (log_cfg.get("level") or level or "INFO").upper()
    log_file = log_cfg.get("log_path")

    logger.remove()
    logger.add(
        sys.stderr,
        format=_DEFAULT_FORMAT_CONSOLE,
        level=effective_level,
        enqueue=True,
        colorize=True,
    )

    if log_file:
        log_dir = os.path.dirname(log_file)
        if log_dir and not os.path.exists(log_dir):
            try:
                os.makedirs(log_dir, exist_ok=True)
            except OSError:
                # 目录建不出来时只保留控制台输出，不让日志系统拖死整个进程
                return
        try:
            logger.add(
                log_file,
                format=_DEFAULT_FORMAT_FILE,
                level=effective_level,
                rotation="10 MB",
                compression="zip",
                enqueue=True,
            )
        except (OSError, PermissionError):
            pass


def setup_logger(name: str, level: str = "INFO"):
    _configure_once(level)
    return logger.bind(name=name)
